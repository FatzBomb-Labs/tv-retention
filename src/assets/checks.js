import { $, el, text } from './dom.js';

// Background checking: the queue that reads series from Sonarr one at a time, the
// poll that follows a run the worker is already doing, and the heartbeat that asks
// what changed. All of its state is private — the queue, the in-flight set, the
// forced set and the timers — which is why this is a factory rather than a set of
// functions: two pages' worth of that state must never exist at once.
//
// Everything it needs from the entry arrives as a callback or an accessor. `monitoring`
// and `snapshot` are handed over as getters because the entry reassigns both wholesale
// (a refresh replaces them), while this module only ever writes *into* them.
export function createChecks({ api, getSnapshot, getMonitoring, applyHealth, applyAlerts,
                               render, renderRules, renderAlerts, renderCounts }) {
  const checking = new Set();
  const forced = new Set();
  let checkQueue = [];
  let checkRunning = false;
  let bulkChecking = false;
  let pollTimer = null;

  const isChecking = (ruleId) => checking.has(ruleId);

  // `force` is what the refresh buttons mean: read this series from Sonarr again. Without
  // it a check re-decides from the episodes already stored, which needs no call at all.
  function queueChecks(ruleIds, force) {
    const wanted = (ruleIds || []).filter((id) => !checkQueue.includes(id) && !checking.has(id));
    if (!wanted.length) return;
    if (force) wanted.forEach((id) => forced.add(id));
    checkQueue = checkQueue.concat(wanted);
    wanted.forEach((id) => checking.add(id));
    // A sweep empties the list: a card left standing during a re-read looks like a
    // result, and it would be a stale one.
    if (checking.size > 1) bulkChecking = true;
    renderRules();
    drainChecks();
  }

  async function drainChecks() {
    if (checkRunning) return;
    checkRunning = true;
    try {
      while (checkQueue.length) {
        const ruleId = checkQueue.shift();
        try {
          const data = await api('check-rule', { rule_id: ruleId, force: forced.has(ruleId) }, '', true);
          if (data.busy) {
            checkQueue.forEach((id) => checking.delete(id));
            checkQueue = [];
            checking.delete(ruleId);
            forced.clear();
            startPolling();
            break;
          }
          getMonitoring()[data.rule_id] = data.state;
          // The alerts arrive with the check that produced them; asking separately cost a
          // second request and a second worker process for every series.
          applyAlerts(data.alerts);
        } catch (error) {
          getMonitoring()[ruleId] = Object.assign({}, getMonitoring()[ruleId], {
            ok: false, label: 'Check failed', error: error.message, checked_at: new Date().toISOString(),
          });
        } finally {
          checking.delete(ruleId);
          forced.delete(ruleId);
          renderRules();
          renderAlerts();
          renderCounts();
        }
      }
    } finally {
      checkRunning = false;
      bulkChecking = false;
      renderRules();
    }
  }

  function startPolling() {
    if (pollTimer) return;
    const tick = async () => {
      try {
        const data = await api('progress', {}, '', true);
        applyHealth(data.health);
        const progress = data.progress || {};
        checking.clear();
        if (progress.running && progress.current) checking.add(progress.current);
        bulkChecking = !!progress.running;
        const fresh = await api('alerts', {}, '', true);
        applyAlerts(fresh.alerts);
        renderRules();
        renderAlerts();
        renderCounts();
        renderCheckBanner(progress);
        if (!progress.running) { clearInterval(pollTimer); pollTimer = null; renderCheckBanner({}); }
      } catch (error) {
        clearInterval(pollTimer);
        pollTimer = null;
        renderCheckBanner({});
      }
    };
    pollTimer = setInterval(tick, 2500);
    tick();
  }

  // -- heartbeat ---------------------------------------------------------
  // One small question to Sonarr — what changed? — and the series it names are re-read
  // through the same queue a manual refresh uses, so each card is seen being read. The
  // cron tick asks the same question every minute whether or not anyone is here, which is
  // why a notification never waits for someone to open this page.
  const WATCH_SECONDS = 15;
  // The heartbeat costs no network at all now — it re-decides every plan from the stored
  // reading, which is what makes a page left open all day still correct about time.
  let watchStamp = '';

  async function watchTick() {
    if (document.hidden || checkRunning || checkQueue.length || pollTimer) return;
    if (!getSnapshot()) return;
    let data;
    try {
      data = await api('watch', {}, '', true);
    } catch (error) {
      return;  // a heartbeat that misses a beat is not worth interrupting anyone for
    }
    if ((data.progress || {}).running) { startPolling(); return; }
    getSnapshot().sync = data.sync || getSnapshot().sync;
    // Re-rendering on a timer would fight with whatever is being read on screen, so it
    // only happens when the reply actually differs from the last one.
    const stamp = JSON.stringify([data.alerts, data.plan, data.stale_rules,
                                  Object.values((data.health || {}).rules || {}).map((r) => r.checked_at)]);
    if (stamp === watchStamp) return;
    watchStamp = stamp;
    applyHealth(data.health);
    applyAlerts(data.alerts);
    getSnapshot().plan = data.plan;
    render();
    if ((data.stale_rules || []).length) queueChecks(data.stale_rules);
  }

  const CHECK_PHASE = { instances: 'verifying the Sonarr instances',
                        matching: 'matching series to Sonarr', rules: 'reading series' };

  function renderCheckBanner(progress) {
    const box = $('tvr-checking');
    box.replaceChildren();
    const active = progress && progress.running;
    box.hidden = !active;
    if (!active) return;
    const phase = CHECK_PHASE[progress.phase] || 'checking';
    let detail = `${progress.scheduled ? 'Scheduled check' : 'Check'} in progress — ${phase}`;
    if (progress.phase === 'rules') {
      detail += `, ${progress.done || 0} of ${progress.total || 0} read`;
      if (progress.current_title) detail += `, now reading ${progress.current_title}`;
    }
    box.append(el('span', { className: 'tvr-spinner' }), text(`${detail}. Series update as they finish.`));
  }

  // The timer and the visibility hook. They cannot run at import — the purity test
  // enforces that for every module but the entry — so the entry calls this at start-up.
  function wire() {
    setInterval(watchTick, WATCH_SECONDS * 1000);
    document.addEventListener('visibilitychange', () => { if (!document.hidden) watchTick(); });
  }

  return { isChecking, queueChecks, startPolling, bulkChecking: () => bulkChecking, wire };
}
