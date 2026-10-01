import { useEffect, useRef, useState } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { X, KeyRound, Upload, Loader2, CheckCircle2, AlertCircle } from 'lucide-react';

const API = (import.meta.env.VITE_API_URL || 'http://localhost:8000').replace(/\/$/, '');
const STORE_KEY = 'ud_youtube_login';

function readSaved() {
  try {
    return JSON.parse(localStorage.getItem(STORE_KEY)) || null;
  } catch {
    return null;
  }
}

async function request(path, options = {}, token) {
  const headers = {};
  if (options.body) headers['Content-Type'] = 'application/json';
  if (token) headers['X-Admin-Token'] = token;

  const res = await fetch(`${API}${path}`, { ...options, headers });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `Request failed (${res.status})`);
  return data;
}

export default function CookiesModal({ open, onClose }) {
  const [token, setToken] = useState(() => readSaved()?.token || '');
  const [cookies, setCookies] = useState(() => readSaved()?.cookies || '');
  const [remember, setRemember] = useState(true);
  const [loaded, setLoaded] = useState(null); // null = checking, true/false = server state
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState(null); // { type: 'ok' | 'err', text }
  const fileRef = useRef(null);

  // On page load: if the server has no cookies (Render restarted or woke up),
  // send the ones saved in this browser again.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const status = await request('/api/cookies/status');
        if (cancelled) return;
        if (status.loaded) {
          setLoaded(true);
          return;
        }
        setLoaded(false);
        const saved = readSaved();
        if (saved?.token && saved?.cookies) {
          await request('/api/cookies', { method: 'POST', body: JSON.stringify({ cookies: saved.cookies }) }, saved.token);
          if (!cancelled) setLoaded(true);
        }
      } catch {
        // backend asleep or unreachable, nothing to do
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // Close on Escape
  useEffect(() => {
    if (!open) return;
    const onKey = (e) => e.key === 'Escape' && onClose();
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, onClose]);

  const onFile = async (e) => {
    const file = e.target.files?.[0];
    if (!file) return;
    setCookies(await file.text());
    setMsg(null);
    e.target.value = '';
  };

  const save = async () => {
    setBusy(true);
    setMsg(null);
    try {
      const out = await request('/api/cookies', { method: 'POST', body: JSON.stringify({ cookies }) }, token.trim());
      setLoaded(true);
      setMsg({ type: 'ok', text: `Saved ${out.count} cookies. Try your link again.` });
      try {
        if (remember) localStorage.setItem(STORE_KEY, JSON.stringify({ token: token.trim(), cookies }));
        else localStorage.removeItem(STORE_KEY);
      } catch {
        // storage blocked, ignore
      }
    } catch (err) {
      setMsg({ type: 'err', text: err.message });
    } finally {
      setBusy(false);
    }
  };

  const clear = async () => {
    setBusy(true);
    setMsg(null);
    try {
      await request('/api/cookies', { method: 'DELETE' }, token.trim());
      setLoaded(false);
      setCookies('');
      try {
        localStorage.removeItem(STORE_KEY);
      } catch {
        // storage blocked, ignore
      }
      setMsg({ type: 'ok', text: 'Cookies removed from the server.' });
    } catch (err) {
      setMsg({ type: 'err', text: err.message });
    } finally {
      setBusy(false);
    }
  };

  const fieldClass =
    'w-full rounded-2xl border border-white/10 bg-slate-900/60 px-4 text-sm text-white placeholder-slate-500 focus:outline-none focus:border-brand-400 transition-colors';

  return (
    <AnimatePresence>
      {open && (
        <motion.div
          className="fixed inset-0 z-[100] flex items-center justify-center p-4"
          style={{ background: 'rgba(2,6,23,0.7)', backdropFilter: 'blur(6px)' }}
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          onClick={onClose}
        >
          <motion.div
            role="dialog"
            aria-modal="true"
            aria-label="YouTube login"
            className="w-full max-w-lg max-h-[90vh] overflow-y-auto p-6 space-y-5"
            style={{
              borderRadius: '24px',
              background: 'rgba(15,23,42,0.92)',
              border: '1px solid rgba(255,255,255,0.1)',
              boxShadow: '0 0 40px rgba(74,222,128,0.12), 0 24px 64px rgba(0,0,0,0.5)',
            }}
            initial={{ opacity: 0, y: 16, scale: 0.97 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: 8, scale: 0.98 }}
            transition={{ type: 'spring', stiffness: 300, damping: 26 }}
            onClick={(e) => e.stopPropagation()}
          >
            {/* Header */}
            <div className="flex items-start justify-between gap-4">
              <div className="flex items-center gap-3">
                <div className="p-2.5 rounded-xl bg-brand-500/15 text-brand-400">
                  <KeyRound className="w-5 h-5" />
                </div>
                <div>
                  <h2 className="text-lg font-bold text-white leading-tight">YouTube login</h2>
                  <p className="text-xs text-slate-400 flex items-center gap-1.5 mt-0.5">
                    <span
                      className="w-1.5 h-1.5 rounded-full"
                      style={{ background: loaded === true ? '#4ade80' : loaded === false ? '#f87171' : '#94a3b8' }}
                    />
                    {loaded === true ? 'Cookies active on the server' : loaded === false ? 'No cookies on the server' : 'Checking server...'}
                  </p>
                </div>
              </div>
              <button
                type="button"
                onClick={onClose}
                aria-label="Close"
                className="p-1.5 text-slate-400 hover:text-white transition-colors rounded-lg focus-visible:outline focus-visible:outline-2 focus-visible:outline-brand-400"
              >
                <X className="w-5 h-5" />
              </button>
            </div>

            <p className="text-sm text-slate-300 leading-relaxed">
              YouTube blocks this server's IP address. Export a <span className="font-mono text-slate-200">cookies.txt</span> from a
              browser signed in to a spare Google account, then add it here.
            </p>

            {/* Token */}
            <div className="space-y-1.5">
              <label htmlFor="ud-token" className="text-xs font-semibold text-slate-400">
                Server token
              </label>
              <input
                id="ud-token"
                type="password"
                autoComplete="off"
                value={token}
                onChange={(e) => setToken(e.target.value)}
                placeholder="The ADMIN_TOKEN you set on Render"
                className={`${fieldClass} h-12`}
              />
            </div>

            {/* Cookies */}
            <div className="space-y-1.5">
              <div className="flex items-center justify-between">
                <label htmlFor="ud-cookies" className="text-xs font-semibold text-slate-400">
                  cookies.txt
                </label>
                <button
                  type="button"
                  onClick={() => fileRef.current?.click()}
                  className="flex items-center gap-1.5 text-xs font-semibold text-brand-400 hover:text-brand-300 transition-colors"
                >
                  <Upload className="w-3.5 h-3.5" />
                  Choose file
                </button>
                <input ref={fileRef} type="file" accept=".txt,text/plain" onChange={onFile} className="hidden" />
              </div>
              <textarea
                id="ud-cookies"
                value={cookies}
                onChange={(e) => setCookies(e.target.value)}
                rows={6}
                spellCheck={false}
                placeholder="Paste the file contents here, or choose the file"
                className={`${fieldClass} py-3 font-mono text-xs resize-none`}
              />
            </div>

            <label className="flex items-start gap-2.5 text-xs text-slate-400 cursor-pointer leading-relaxed">
              <input
                type="checkbox"
                checked={remember}
                onChange={(e) => setRemember(e.target.checked)}
                className="mt-0.5 accent-[#4ade80]"
              />
              <span>Keep these in this browser and re-send them automatically when the server restarts.</span>
            </label>

            {/* Message */}
            <AnimatePresence>
              {msg && (
                <motion.div
                  initial={{ opacity: 0, height: 0 }}
                  animate={{ opacity: 1, height: 'auto' }}
                  exit={{ opacity: 0, height: 0 }}
                  className="overflow-hidden"
                >
                  <div
                    className={`p-3.5 rounded-2xl flex items-start gap-2.5 text-sm font-medium border ${
                      msg.type === 'ok'
                        ? 'bg-brand-500/10 border-brand-500/30 text-brand-400'
                        : 'bg-red-500/10 border-red-500/30 text-red-400'
                    }`}
                  >
                    {msg.type === 'ok' ? <CheckCircle2 className="w-4 h-4 shrink-0 mt-0.5" /> : <AlertCircle className="w-4 h-4 shrink-0 mt-0.5" />}
                    <span>{msg.text}</span>
                  </div>
                </motion.div>
              )}
            </AnimatePresence>

            {/* Actions */}
            <div className="flex gap-3 pt-1">
              <button
                type="button"
                onClick={save}
                disabled={busy || !token.trim() || !cookies.trim()}
                className="flex-1 h-12 rounded-2xl bg-brand-500 text-dark-900 font-bold border border-brand-400 flex items-center justify-center gap-2 disabled:opacity-50 disabled:pointer-events-none shadow-[0_0_20px_rgba(74,222,128,0.25)] hover:shadow-[0_0_28px_rgba(74,222,128,0.5)] transition-shadow"
              >
                {busy ? <Loader2 className="w-4 h-4 animate-spin" /> : null}
                Save cookies
              </button>
              {loaded && (
                <button
                  type="button"
                  onClick={clear}
                  disabled={busy || !token.trim()}
                  className="h-12 px-5 rounded-2xl border border-white/15 text-slate-300 font-semibold hover:border-red-400/60 hover:text-red-400 transition-colors disabled:opacity-50 disabled:pointer-events-none"
                >
                  Remove
                </button>
              )}
            </div>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}
