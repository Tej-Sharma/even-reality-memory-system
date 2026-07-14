// Persistent token storage. Prefers the Even SDK local storage (survives WebView
// suspension, app kill, and updates per the docs); falls back to window.localStorage
// when running in a plain browser for preview.
import type { Bridge } from './glasses';

const TOKEN_KEY = 'constella_access_token';
const TENANT_KEY = 'constella_tenant_name';

let bridge: Bridge | null = null;

export function bindStore(b: Bridge) {
  bridge = b;
}

async function get(key: string): Promise<string | null> {
  try {
    if (bridge?.getLocalStorage) {
      const v = await bridge.getLocalStorage(key);
      return v ?? null;
    }
  } catch {
    /* fall through to browser storage */
  }
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

async function set(key: string, value: string): Promise<void> {
  try {
    if (bridge?.setLocalStorage) {
      await bridge.setLocalStorage(key, value);
      return;
    }
  } catch {
    /* fall through */
  }
  try {
    window.localStorage.setItem(key, value);
  } catch {
    /* ignore */
  }
}

export const getToken = () => get(TOKEN_KEY);
export const getTenant = () => get(TENANT_KEY);

// Last-used view ('quick' | 'meeting') so the app boots one tap from the
// user's actual habit (meeting-heavy users land on MEETING home).
export const saveView = (view: string) => set('constella_view', view);

export async function saveSession(token: string, tenant: string) {
  await set(TOKEN_KEY, token);
  await set(TENANT_KEY, tenant || '');
}

export async function clearSession() {
  await set(TOKEN_KEY, '');
  await set(TENANT_KEY, '');
}
