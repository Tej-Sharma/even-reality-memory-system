// Thin client for the backend /glasses/* endpoints. Auth uses the custom
// `access-token` header (same scheme as the rest of the Constella API).
import { API_BASE } from './config';
import { getToken } from './store';

export interface MemoryResult {
  uniqueid: string | null;
  title: string;
  snippet: string;
  score: number | null;
}
export interface QueryResponse {
  results: MemoryResult[];
  display_text: string;
  count: number;
  query?: string;
  transcript?: string;
}
export interface DeviceStart {
  device_code: string;
  user_code: string;
  verification_url: string;
  interval: number;
  expires_in: number;
}
export interface DevicePoll {
  status: 'pending' | 'approved' | 'expired';
  token?: string;
  tenant_name?: string;
  email?: string;
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

async function post<T>(path: string, body: unknown, auth = true): Promise<T> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  if (auth) {
    const token = await getToken();
    if (token) headers['access-token'] = token;
  }
  const res = await fetch(`${API_BASE}${path}`, {
    method: 'POST',
    headers,
    body: JSON.stringify(body ?? {}),
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      detail = (await res.json())?.detail || detail;
    } catch {
      /* ignore */
    }
    throw new ApiError(res.status, detail);
  }
  return (await res.json()) as T;
}

// --- Device-link login (public) ---
export const deviceStart = () =>
  post<DeviceStart>('/glasses/auth/device/start', {}, false);
export const devicePoll = (device_code: string) =>
  post<DevicePoll>('/glasses/auth/device/poll', { device_code }, false);

// --- Authenticated ---
// /glasses/me is a GET, so it's fetched directly rather than via post().
export async function fetchMe(): Promise<{ tenant_name: string; email: string }> {
  const token = await getToken();
  const res = await fetch(`${API_BASE}/glasses/me`, {
    headers: token ? { 'access-token': token } : {},
  });
  if (!res.ok) throw new ApiError(res.status, res.statusText);
  return (await res.json()) as { tenant_name: string; email: string };
}

export const askAudio = (pcm_base64: string, sample_rate: number, top_k = 8) =>
  post<QueryResponse>('/glasses/ask-audio', { pcm_base64, sample_rate, top_k });

export const noteAudio = (pcm_base64: string, sample_rate: number, title?: string) =>
  post<{ transcript: string; ok: boolean; uniqueid: string | null; title: string | null }>(
    '/glasses/note-audio',
    { pcm_base64, sample_rate, title }
  );

// Unified one-tap endpoint: backend transcribes, classifies ask-vs-note, acts.
export interface VoiceResponse {
  intent: 'ask' | 'note' | 'none';
  transcript: string;
  // ask fields
  display_text?: string;
  results?: MemoryResult[];
  count?: number;
  // note fields
  ok?: boolean;
  title?: string | null;
  uniqueid?: string | null;
}
export const voiceAudio = (pcm_base64: string, sample_rate: number, top_k = 8) =>
  post<VoiceResponse>('/glasses/voice', { pcm_base64, sample_rate, top_k });

// --- Meeting mode: continuous transcription into one searchable memory ---
export const meetingStart = (title?: string) =>
  post<{ meeting_id: string }>('/glasses/meeting/start', { title });

export const meetingChunk = (
  meeting_id: string,
  pcm_base64: string,
  sample_rate: number,
  seq: number
) =>
  post<{ text: string; seq: number }>('/glasses/meeting/chunk', {
    meeting_id,
    pcm_base64,
    sample_rate,
    seq,
  });

// Poll for the newest live AI cue (server generates them as speech accrues).
export const meetingCue = (meeting_id: string, after_seq: number) =>
  post<{ cue: { seq: number; text: string; ts: number } | null }>(
    '/glasses/meeting/cue',
    { meeting_id, after_seq }
  );

export const meetingEnd = (
  meeting_id: string,
  pcm_base64?: string,
  sample_rate?: number,
  title?: string
) =>
  post<{ ok: boolean; title: string | null; uniqueid: string | null; chars: number }>(
    '/glasses/meeting/end',
    { meeting_id, pcm_base64, sample_rate, title }
  );
