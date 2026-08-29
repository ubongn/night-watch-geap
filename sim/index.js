#!/usr/bin/env node
/**
 * Meridian Freight — night-shift simulator.
 *
 * Two planes in one process:
 *   1. DATA PLANE  — live telemetry for five services. Values follow healthy
 *      baselines until a fault is injected, then follow the fault signature
 *      (the same signatures the Diagnostician reasons over). Exposed as
 *      GET /snapshot?service=<s>  (evidence packs: metrics + logs)
 *      GET /metrics              (Prometheus text exposition, scrapable)
 *   2. CONTROL PLANE (action plane) — authenticated API Night Watch's
 *      Executor writes to; the eval harness and the demo inject faults here.
 *      POST /control/fault   {kind, target, params}
 *      POST /control/repair  {action, params}   -> {"detail": "..."}
 *      POST /control/clear   {target}
 *      GET  /control/faults
 *
 * No dependencies: node:http only. The night shift also ticks to stdout so
 * `docker logs` / Cloud Run logs show a living operation.
 */

'use strict';

const http = require('node:http');
const crypto = require('node:crypto');

const PORT = parseInt(process.env.PORT || '7822', 10);
const TOKEN = process.env.SIM_TOKEN || 'night-watch-dev';
const TICK_MS = parseInt(process.env.TICK_MS || '5000', 10);

const SERVICES = [
  'dispatch-api',
  'sorter-conveyor',
  'wms-postgres',
  'gps-ingest',
  'charge-docks',
];

// fault kinds mirror night_watch.models.FaultClass
const FAULT_KINDS = [
  'api_latency',
  'conveyor_jam',
  'db_connection_exhaustion',
  'ingest_backlog',
  'dock_fault',
];

// action -> fault class it actually remediates (mirrors actions.REGISTRY.neutralizes)
const REPAIRS = {
  restart_service: ['api_latency', 'ingest_backlog'],
  clear_jam: ['conveyor_jam'],
  drain_dock: ['dock_fault'],
  roll_worker_pool: ['db_connection_exhaustion'],
  throttle_ingest: ['ingest_backlog'],
};

const state = {
  faults: new Map(), // target -> {kind, since, params, repairs: n}
  repairs: [], // [{action, target, at}]
  startedAt: Date.now(),
  tick: 0,
};

// --------------------------------------------------------------------------
// telemetry model
// --------------------------------------------------------------------------

const wiggle = (base, spread) =>
  base + (Math.sin(Date.now() / 9700 + Math.random() * 0.4) * spread);

/** Sample window (4 points) for a service, honoring any active fault. */
function samples(service) {
  const fault = [...state.faults.values()].find((f) => f.target === service) ||
    [...state.faults.values()].find((f) => f.target.split(':')[0] === service);

  if (!fault) {
    // healthy baselines
    if (service === 'sorter-conveyor') {
      return {
        metrics: [
          { query: 'meridian_conveyor_jam_seconds', values: [0, 0, 0, 0], unit: 's' },
          { query: 'meridian_sorter_throughput', values: [880, 905, 895, 910].map((v) => wiggle(v, 12)), unit: 'items/min' },
        ],
        logs: ['ok: sorter cycle complete', 'ok: dock handshake clean'],
      };
    }
    if (service === 'wms-postgres') {
      return {
        metrics: [
          { query: 'pg_pool_saturation', values: [0.31, 0.34, 0.33, 0.36].map((v) => wiggle(v, 0.01)), unit: 'ratio' },
        ],
        logs: ['ok: checkpoint complete', 'ok: autovacuum finished'],
      };
    }
    if (service === 'gps-ingest') {
      return {
        metrics: [
          { query: 'ingest_backlog_seconds', values: [3, 4, 3, 5].map((v) => wiggle(v, 0.6)), unit: 's' },
        ],
        logs: ['ok: batch flushed'],
      };
    }
    if (service === 'charge-docks') {
      return {
        metrics: [
          { query: 'dock_error_rate', values: [0.0, 0.01, 0.0, 0.01], unit: 'ratio' },
        ],
        logs: ['ok: dock charge cycle nominal'],
      };
    }
    return {
      metrics: [
        { query: 'error_rate', values: [0.01, 0.0, 0.01, 0.01], unit: 'ratio' },
        { query: 'p99_latency', values: [218, 224, 221, 226].map((v) => wiggle(v, 4)), unit: 'ms' },
      ],
      logs: ['ok: request completed'],
    };
  }

  switch (fault.kind) {
    case 'conveyor_jam': {
      const dock = (fault.params && fault.params.dock) || fault.target.split(':')[1] || 'dock-3';
      return {
        metrics: [
          { query: 'meridian_conveyor_jam_seconds', values: ramp(40, 260, 240), unit: 's', labels: { dock } },
          { query: 'meridian_sorter_throughput', values: ramp(880, 20, 180), unit: 'items/min' },
        ],
        logs: [`ERROR motor_overtemp ${dock}`, `ERROR jam_detected ${dock}`],
      };
    }
    case 'api_latency':
      return {
        metrics: [
          { query: 'p99_latency', values: ramp(900, 3800, 300), unit: 'ms' },
          { query: 'error_rate', values: ramp(0.08, 0.22, 300), unit: 'ratio' },
        ],
        logs: ['WARN upstream slow', 'ERROR timeout awaiting dispatch core'],
      };
    case 'db_connection_exhaustion':
      return {
        metrics: [
          { query: 'pg_pool_saturation', values: ramp(0.9, 0.99, 240), unit: 'ratio' },
          { query: 'p99_latency', values: ramp(400, 1500, 240), unit: 'ms' },
        ],
        logs: ['ERROR connection pool exhausted', 'FATAL: too many connections'],
      };
    case 'ingest_backlog':
      return {
        metrics: [
          { query: 'ingest_backlog_seconds', values: ramp(120, 900, 400), unit: 's' },
          { query: 'ingest_drop_rate', values: ramp(0.0, 0.05, 400), unit: 'ratio' },
        ],
        logs: ['WARN consumer lag growing', 'ERROR gps batch dropped (backpressure)'],
      };
    case 'dock_fault': {
      const dock = (fault.params && fault.params.dock) || fault.target.split(':')[1] || 'dock-2';
      return {
        metrics: [
          { query: 'dock_error_rate', values: ramp(0.2, 0.45, 200), unit: 'ratio', labels: { dock } },
          { query: 'dock_charge_current', values: ramp(40, 3, 200), unit: 'A', labels: { dock } },
        ],
        logs: [`ERROR charger fault code E17 ${dock}`, `WARN dock offline retry ${dock}`],
      };
    }
    default:
      return {
        metrics: [{ query: 'saturation', values: [0.9, 0.97], unit: 'ratio' }],
        logs: [`ERROR ${fault.kind}`],
      };
  }
}

/** Expand a scalar ramp start into a 4-point window ending at the ramp value. */
function ramp(lo, hi, over) {
  const t = (Date.now() - (state.faultActiveSince || Date.now())) / 1000;
  const end = Math.min(hi, lo + (hi - lo) * Math.min(1, t / over));
  return [lo, lo + (end - lo) * 0.4, lo + (end - lo) * 0.75, Number(end.toFixed(2))];
}

// --------------------------------------------------------------------------
// control plane
// --------------------------------------------------------------------------

function authorized(req) {
  const hdr = req.headers['authorization'] || '';
  return hdr === `Bearer ${TOKEN}`;
}

function applyRepair(action, params) {
  const fixes = REPAIRS[action];
  if (!fixes) return { ok: false, status: 400, detail: `unknown action ${action}` };
  const target = params.service || params.dock || params.topic || '';
  let fixed = 0;
  for (const [key, fault] of [...state.faults.entries()]) {
    if (fixes.includes(fault.kind) && (target === '' || key.includes(target))) {
      state.faults.delete(key);
      fixed += 1;
    }
  }
  state.repairs.push({ action, target, at: new Date().toISOString() });
  return {
    ok: true,
    detail: `${action} applied to ${JSON.stringify(params)} — ${fixed} fault(s) cleared`,
  };
}

// --------------------------------------------------------------------------
// snapshot / evidence packs
// --------------------------------------------------------------------------

function snapshot(service) {
  // find fault: faults are keyed "service[:component]"
  let fault = null;
  for (const f of state.faults.values()) {
    if (f.target === service || f.target.split(':')[0] === service) { fault = f; break; }
  }
  if (fault) state.faultActiveSince = fault.since;
  const s = samples(service);
  return {
    service,
    fault: fault ? { kind: fault.kind, since: fault.since } : null,
    metrics: s.metrics.map((m) => ({
      query: m.query,
      labels: Object.assign({ service }, m.labels || {}),
      values: m.values.map((v) => Number(Number(v).toFixed(3))),
      unit: m.unit,
    })),
    logs: {
      query: `{service="${service}"}`,
      lines: s.logs,
      service,
    },
    taken_at: new Date().toISOString(),
  };
}

// --------------------------------------------------------------------------
// http plumbing
// --------------------------------------------------------------------------

function readBody(req) {
  return new Promise((resolve, reject) => {
    let data = '';
    req.on('data', (c) => { data += c; if (data.length > 1e6) req.destroy(); });
    req.on('end', () => {
      try { resolve(data ? JSON.parse(data) : {}); } catch (e) { reject(e); }
    });
    req.on('error', reject);
  });
}

function json(res, code, body) {
  const payload = JSON.stringify(body);
  res.writeHead(code, { 'content-type': 'application/json; charset=utf-8' });
  res.end(payload);
}

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, `http://${req.headers.host || 'localhost'}`);
  const path = url.pathname;

  try {
    if (path === '/healthz') {
      return json(res, 200, {
        ok: true,
        service: 'meridian-sim',
        faults: state.faults.size,
        repairs: state.repairs.length,
        uptime_s: Math.round((Date.now() - state.startedAt) / 1000),
      });
    }

    if (path === '/' && req.method === 'GET') {
      res.writeHead(200, { 'content-type': 'text/html; charset=utf-8' });
      return res.end(statusPage());
    }

    if (path === '/metrics' && req.method === 'GET') {
      const lines = [];
      for (const svc of SERVICES) {
        const snap = snapshot(svc);
        for (const m of snap.metrics) {
          const name = m.query.replace(/[^a-z_]/gi, '_');
          lines.push(`${name}{service="${svc}"} ${m.values[m.values.length - 1]}`);
        }
      }
      res.writeHead(200, { 'content-type': 'text/plain; version=0.0.4' });
      return res.end(lines.join('\n') + '\n');
    }

    if (path === '/snapshot' && req.method === 'GET') {
      const svc = url.searchParams.get('service');
      if (!svc) return json(res, 400, { error: 'service query param required' });
      if (!SERVICES.includes(svc)) return json(res, 404, { error: `unknown service ${svc}` });
      return json(res, 200, snapshot(svc));
    }

    if (path === '/control/faults' && req.method === 'GET') {
      if (!authorized(req)) return json(res, 401, { error: 'unauthorized' });
      return json(res, 200, { faults: [...state.faults.values()] });
    }

    if (path.startsWith('/control/') && req.method === 'POST') {
      if (!authorized(req)) return json(res, 401, { error: 'unauthorized' });
      const body = await readBody(req);

      if (path === '/control/fault') {
        const { kind, target, params } = body;
        if (!FAULT_KINDS.includes(kind)) {
          return json(res, 400, { error: `unknown fault kind ${kind}` });
        }
        const key = `${target || kind}:${(params && (params.dock || params.service || params.topic)) || kind}`;
        state.faults.set(key, {
          kind, target: target || kind, params: params || {},
          since: Date.now(), injected_at: new Date().toISOString(),
        });
        console.log(`[fault] injected ${kind} on ${target || kind} ${JSON.stringify(params || {})}`);
        return json(res, 200, { ok: true, target: target || kind, kind });
      }

      if (path === '/control/repair') {
        const { action, params } = body;
        const out = applyRepair(action, params || {});
        if (out.ok) console.log(`[repair] ${out.detail}`);
        return json(res, out.ok ? 200 : out.status, out);
      }

      if (path === '/control/clear') {
        const { target } = body;
        let n = 0;
        for (const key of [...state.faults.keys()]) {
          if (!target || key.includes(target)) { state.faults.delete(key); n += 1; }
        }
        return json(res, 200, { ok: true, cleared: n });
      }

      return json(res, 404, { error: 'unknown control endpoint' });
    }

    return json(res, 404, { error: `no route ${path}` });
  } catch (err) {
    return json(res, 500, { error: String(err && err.message ? err.message : err) });
  }
});

// --------------------------------------------------------------------------
// the night shift ticks
// --------------------------------------------------------------------------

setInterval(() => {
  state.tick += 1;
  const faults = state.faults.size;
  const thr = Math.round(
    (snapshot('sorter-conveyor').metrics[1] || { values: [0] }).values.slice(-1)[0]
  );
  console.log(
    `[tick ${String(state.tick).padStart(4, '0')}] parcels moving — sorter ${thr} items/min, ` +
    `active faults: ${faults}, repairs tonight: ${state.repairs.length}`
  );
}, TICK_MS);

function statusPage() {
  const rows = SERVICES.map((svc) => {
    const snap = snapshot(svc);
    const fault = snap.fault ? `FAULT ${snap.fault.kind}` : 'nominal';
    return `<tr><td>${svc}</td><td>${fault}</td><td>${snap.metrics
      .map((m) => `${m.query}=${m.values[m.values.length - 1]}${m.unit}`)
      .join(', ')}</td></tr>`;
  }).join('');
  return `<!doctype html><html><head><meta charset="utf-8"><meta http-equiv="refresh" content="5">
<title>Meridian Freight — night shift</title>
<style>body{font-family:ui-monospace,Consolas,monospace;background:#0b1020;color:#dbe4ff;margin:2rem}
h1{color:#ffb454}table{border-collapse:collapse}td,th{border:1px solid #2a3558;padding:.4rem .8rem}
th{color:#8fa3d9}small{color:#67749b}</style></head><body>
<h1>Meridian Freight — night shift (simulator)</h1>
<p><small>data plane + control plane — GET /snapshot?service=..., POST /control/* (Bearer)</small></p>
<table><tr><th>service</th><th>state</th><th>latest samples</th></tr>${rows}</table>
<p><small>repairs tonight: ${state.repairs.length} — uptime ${Math.round((Date.now() - state.startedAt) / 1000)}s</small></p>
</body></html>`;
}

server.listen(PORT, () => {
  console.log(`meridian-sim listening on :${PORT} (control plane auth: Bearer token)`);
});

module.exports = { server, state, snapshot };
