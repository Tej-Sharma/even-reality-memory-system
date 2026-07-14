'use client';

// /glasses/link — the phone-browser login page for the device-link flow. The
// glasses show a short code + this URL; the user enters their Constella email,
// gets a 6-digit code by email, and links the device. Calls the backend
// (fastfind.app) which is CORS-allowlisted for constella.app.
import { useEffect, useState } from 'react';

const API_BASE = (
  process.env.NEXT_PUBLIC_API_BASE || 'https://fastfind.app'
).replace(/\/$/, '');

type Step = 'email' | 'code' | 'done';

export default function GlassesLinkPage() {
  const [step, setStep] = useState<Step>('email');
  const [email, setEmail] = useState('');
  const [code, setCode] = useState('');
  const [userCode, setUserCode] = useState('');
  const [msg, setMsg] = useState<{ text: string; ok: boolean } | null>(null);
  const [busy, setBusy] = useState(false);
  const [agent, setAgent] = useState<{ url: string; token: string } | null>(null);
  const [copied, setCopied] = useState('');

  // Prefill the glasses code from ?code= without needing a Suspense boundary.
  useEffect(() => {
    const p = new URLSearchParams(window.location.search).get('code');
    if (p) setUserCode(p.toUpperCase());
  }, []);

  async function sendCode() {
    const e = email.trim().toLowerCase();
    if (!e || !e.includes('@')) {
      setMsg({ text: 'Enter a valid email.', ok: false });
      return;
    }
    setBusy(true);
    setMsg({ text: 'Sending...', ok: true });
    try {
      const res = await fetch(`${API_BASE}/glasses/auth/link/request-code`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email: e }),
      });
      if (!res.ok) throw new Error('request failed');
      setEmail(e);
      setStep('code');
      setMsg({ text: `Code sent to ${e}`, ok: true });
    } catch {
      setMsg({ text: 'Could not send code. Try again.', ok: false });
    } finally {
      setBusy(false);
    }
  }

  async function verify() {
    if (code.trim().length < 6) {
      setMsg({ text: 'Enter the 6-digit code.', ok: false });
      return;
    }
    // Glasses code is optional: without it this becomes an Even AI agent-token
    // login (Settings -> Even AI -> Add Agent in the Even app).
    setBusy(true);
    setMsg({ text: 'Linking...', ok: true });
    try {
      const res = await fetch(`${API_BASE}/glasses/auth/link/verify`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          user_code: userCode.trim(),
          email: email,
          code: code.trim(),
        }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        setMsg({ text: data.detail || 'Invalid code.', ok: false });
        return;
      }
      if (data.agent_url && data.agent_token) {
        setAgent({ url: data.agent_url, token: data.agent_token });
      }
      setStep('done');
    } catch {
      setMsg({ text: 'Something went wrong. Try again.', ok: false });
    } finally {
      setBusy(false);
    }
  }

  async function copy(label: string, value: string) {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(label);
      setTimeout(() => setCopied(''), 1500);
    } catch {
      /* clipboard unavailable — the value is visible to copy manually */
    }
  }

  return (
    <div style={S.wrap}>
      <div style={S.card}>
        <h1 style={S.h1}>Link your glasses</h1>
        <p style={S.sub}>
          Sign in with your Constella email to connect your smart glasses to your
          memory.
        </p>

        {step === 'email' && (
          <>
            <label style={S.label}>Email</label>
            <input
              style={S.input}
              type='email'
              inputMode='email'
              autoComplete='email'
              placeholder='you@example.com'
              value={email}
              onChange={(e) => setEmail(e.target.value)}
            />
            <button style={S.button} disabled={busy} onClick={sendCode}>
              Send code
            </button>
          </>
        )}

        {step === 'code' && (
          <>
            <label style={S.label}>6-digit code (check your email)</label>
            <input
              style={{ ...S.input, ...S.codeInput }}
              inputMode='numeric'
              autoComplete='one-time-code'
              maxLength={6}
              placeholder='123456'
              value={code}
              onChange={(e) => setCode(e.target.value)}
            />
            <label style={S.label}>
              Code shown on your glasses (leave empty if you only want the Even
              AI agent token)
            </label>
            <input
              style={S.input}
              placeholder='XXXX-XXXX (optional)'
              value={userCode}
              onChange={(e) => setUserCode(e.target.value.toUpperCase())}
            />
            <button style={S.button} disabled={busy} onClick={verify}>
              Verify &amp; link
            </button>
          </>
        )}

        {step === 'done' && (
          <>
            <h2 style={{ ...S.h1, fontSize: 18, marginTop: 8 }}>All set</h2>
            <p style={S.sub}>
              {userCode.trim()
                ? "Your glasses are linked. Head back to them, they'll connect automatically."
                : 'Signed in. Set up the Even AI agent below for instant hands-free capture.'}
            </p>
            {agent && (
              <>
                <h3 style={{ ...S.h1, fontSize: 15, marginTop: 20 }}>
                  Hands-free: add Constella to Even AI
                </h3>
                <p style={S.sub}>
                  In the Even Realities app: Settings → Even AI → Add Agent.
                  Then just hold the left temple and speak — Constella saves
                  thoughts and answers questions from your memory.
                </p>
                <label style={S.label}>Agent URL</label>
                <div style={S.copyRow}>
                  <code style={S.code}>{agent.url}</code>
                  <button style={S.copyBtn} onClick={() => copy('url', agent.url)}>
                    {copied === 'url' ? 'Copied' : 'Copy'}
                  </button>
                </div>
                <label style={S.label}>Agent token</label>
                <div style={S.copyRow}>
                  <code style={S.code}>
                    {agent.token.slice(0, 18)}…{agent.token.slice(-6)}
                  </code>
                  <button
                    style={S.copyBtn}
                    onClick={() => copy('token', agent.token)}
                  >
                    {copied === 'token' ? 'Copied' : 'Copy'}
                  </button>
                </div>
                <label style={S.label}>Name</label>
                <div style={S.copyRow}>
                  <code style={S.code}>Constella</code>
                  <button
                    style={S.copyBtn}
                    onClick={() => copy('name', 'Constella')}
                  >
                    {copied === 'name' ? 'Copied' : 'Copy'}
                  </button>
                </div>
              </>
            )}
          </>
        )}

        {msg && step !== 'done' && (
          <div style={{ ...S.msg, color: msg.ok ? '#67d98b' : '#ff7a7a' }}>
            {msg.text}
          </div>
        )}
      </div>
    </div>
  );
}

const S: Record<string, React.CSSProperties> = {
  wrap: {
    minHeight: '100vh',
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    background: '#0f1115',
    color: '#e9ecf1',
    padding: 24,
    fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif',
  },
  card: {
    width: '100%',
    maxWidth: 380,
    background: '#171a21',
    border: '1px solid #262b36',
    borderRadius: 16,
    padding: '28px 24px',
  },
  h1: { fontSize: 20, margin: '0 0 4px' },
  sub: { margin: '0 0 20px', color: '#99a2b2', fontSize: 14, lineHeight: 1.45 },
  label: { display: 'block', fontSize: 13, color: '#b7bfcc', margin: '14px 0 6px' },
  input: {
    width: '100%',
    padding: '12px 14px',
    fontSize: 16,
    borderRadius: 10,
    border: '1px solid #2d333f',
    background: '#0f1216',
    color: '#e9ecf1',
    boxSizing: 'border-box',
  },
  codeInput: { letterSpacing: 4, textAlign: 'center', fontSize: 20 },
  copyRow: { display: 'flex', gap: 8, alignItems: 'center', marginBottom: 4 },
  code: {
    flex: 1,
    fontSize: 12,
    padding: '8px 10px',
    borderRadius: 8,
    border: '1px solid #2d333f',
    background: '#0f1216',
    color: '#b7bfcc',
    overflowWrap: 'anywhere',
  },
  copyBtn: {
    padding: '8px 12px',
    fontSize: 13,
    fontWeight: 600,
    border: 'none',
    borderRadius: 8,
    background: '#2d333f',
    color: '#e9ecf1',
    cursor: 'pointer',
    whiteSpace: 'nowrap',
  },
  button: {
    width: '100%',
    marginTop: 18,
    padding: 13,
    fontSize: 15,
    fontWeight: 600,
    border: 'none',
    borderRadius: 10,
    background: '#4c8bf5',
    color: '#fff',
    cursor: 'pointer',
  },
  msg: { marginTop: 14, fontSize: 13, minHeight: 18 },
};
