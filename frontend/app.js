// Shared client for every page. Each page opens its own socket, rejoins the room
// from sessionStorage, and the server replays the screen that page should show.
const AQ = (() => {
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const getSession = () => JSON.parse(sessionStorage.getItem('aq') || 'null');
  const setSession = (s) => sessionStorage.setItem('aq', JSON.stringify(s));
  const clearSession = () => sessionStorage.removeItem('aq');
  const PAGE = { lobby: 'lobby.html', question: 'quiz.html', reveal: 'quiz.html', final: 'leaderboard.html' };
  const here = () => location.pathname.split('/').pop() || 'index.html';

  let ws, handlers = {}, queue = [], retry = 0, rejoin = true;

  function send(obj) {
    const text = JSON.stringify(obj);
    if (ws && ws.readyState === 1) ws.send(text); else queue.push(text);
  }

  function open() {
    ws = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws');
    ws.onopen = () => {
      retry = 0;
      const s = getSession();
      if (rejoin && s) ws.send(JSON.stringify({ type: 'rejoin', role: s.role, code: s.code, key: s.key }));
      queue.splice(0).forEach((t) => ws.send(t));
      if (handlers.open) handlers.open();
    };
    ws.onmessage = (e) => {
      const m = JSON.parse(e.data);
      const target = PAGE[m.type];
      if (target && getSession() && target !== here()) { location.replace(target); return; }
      if (m.type === 'error' && m.fatal) { clearSession(); location.replace('index.html'); return; }
      if (m.type === 'kicked' || m.type === 'closed') {
        clearSession();
        alert(m.type === 'kicked' ? 'The host removed you from this room.' : 'The host closed this game.');
        location.replace('index.html');
        return;
      }
      if (handlers[m.type]) handlers[m.type](m);
    };
    ws.onclose = () => setTimeout(open, Math.min(3000, 400 * 2 ** retry++));
  }

  function connect(h, opts = {}) {
    handlers = h;
    rejoin = opts.rejoin !== false;
    open();
  }

  async function api(path, method = 'GET', body) {
    const res = await fetch(path, {
      method,
      headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + (localStorage.getItem('aq_token') || '') },
      body: body ? JSON.stringify(body) : undefined,
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const err = new Error(typeof data.detail === 'string' ? data.detail : 'Something went wrong.');
      err.status = res.status;
      throw err;
    }
    return data;
  }

  return { $, esc, getSession, setSession, clearSession, connect, send, api };
})();
