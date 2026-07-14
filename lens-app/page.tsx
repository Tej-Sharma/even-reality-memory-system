'use client';

// /glasses — the Even G2 glasses app. Loaded client-only (ssr: false) because the
// Even Hub SDK bridge and browser APIs (localStorage, atob, fetch) must not run on
// the server. Runs inside the Even Realities app WebView; renders on the lens.
import dynamic from 'next/dynamic';

const GlassesApp = dynamic(() => import('./GlassesApp'), { ssr: false });

export default function GlassesPage() {
  return <GlassesApp />;
}
