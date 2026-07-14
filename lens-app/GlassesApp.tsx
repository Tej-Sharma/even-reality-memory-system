'use client';

// Client-only entry for /glasses. Detects the environment:
// - Inside the Even Realities WebView -> boot the lens app.
// - Normal browser -> render the setup/instructions page (Docs).
//
// Detection relies ONLY on window.flutter_inappwebview: that handle is
// injected by the Even app's WebView layer (both iOS and Android). Do NOT
// check window.EvenAppBridge / _listenEvenAppMessage — the Even Hub SDK
// creates those itself on import, which false-positives in normal browsers.
// The lens app module (and with it the SDK) is imported lazily only after the
// WebView is confirmed, so the SDK can't pollute globals before detection.
import { useEffect, useRef, useState } from 'react';

import Docs from './Docs';

type Env = 'detecting' | 'glasses' | 'browser';

function hostWebViewPresent(): boolean {
  return Boolean((window as any).flutter_inappwebview);
}

export default function GlassesApp() {
  const started = useRef(false);
  const [env, setEnv] = useState<Env>('detecting');

  useEffect(() => {
    if (started.current) return;
    started.current = true;

    const boot = async () => {
      setEnv('glasses');
      const { startGlassesApp } = await import('./lib/app');
      void startGlassesApp();
    };

    // ?demo=1 forces the lens app (used in the Even simulator to capture store
    // screenshots via scripted screens — see startGlassesApp's demo branch).
    try {
      if (new URLSearchParams(window.location.search).get('demo') === '1') {
        void boot();
        return;
      }
    } catch {
      /* ignore */
    }

    if (hostWebViewPresent()) {
      void boot();
      return;
    }
    // Poll up to ~1.5s for late injection, then assume plain browser.
    let waited = 0;
    const timer = setInterval(() => {
      if (hostWebViewPresent()) {
        clearInterval(timer);
        void boot();
        return;
      }
      waited += 150;
      if (waited >= 1500) {
        clearInterval(timer);
        setEnv('browser');
      }
    }, 150);
    return () => clearInterval(timer);
  }, []);

  if (env === 'browser') return <Docs />;

  return (
    <div
      style={{
        minHeight: '100vh',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        textAlign: 'center',
        padding: 24,
        background: '#0f1115',
        color: '#99a2b2',
        fontFamily:
          '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif',
      }}
    >
      <div style={{ maxWidth: 320, fontSize: 14, lineHeight: 1.5 }}>
        {env === 'glasses'
          ? 'Constella is running on your glasses.'
          : 'Loading...'}
      </div>
    </div>
  );
}
