// Device-link login for a keyboardless device. The glasses show a short code +
// URL; the user finishes the email login in their phone browser; we poll until
// the backend hands back a token, which we persist.
import { devicePoll,deviceStart } from './api';
import { render } from './glasses';
import { saveSession } from './store';

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

function loginScreen(userCode: string): string {
  // Terse, no emoji (the lens can't render them). Short host to fit the display.
  return [
    'Link your glasses',
    '',
    'On your phone, open:',
    'constella.app/glasses/link',
    '',
    `Code: ${userCode}`,
    '',
    'Waiting for you...',
  ].join('\n');
}

/** Run the full login handshake. Resolves once a token is stored. Loops forever
 *  across expiries until the user completes it. */
export async function runLogin(): Promise<{ tenant: string; email?: string }> {
  for (;;) {
    let session;
    try {
      session = await deviceStart();
    } catch {
      await render("Can't reach Constella.\nCheck connection.\nTap to retry.");
      await sleep(4000);
      continue;
    }

    await render(loginScreen(session.user_code));

    const deadline = Date.now() + session.expires_in * 1000;
    const interval = Math.max(2, session.interval) * 1000;

    while (Date.now() < deadline) {
      await sleep(interval);
      let poll;
      try {
        poll = await devicePoll(session.device_code);
      } catch {
        continue; // transient; keep polling
      }
      if (poll.status === 'approved' && poll.token) {
        await saveSession(poll.token, poll.tenant_name || '');
        await render("Linked! You're in.");
        await sleep(1200);
        return { tenant: poll.tenant_name || '', email: poll.email };
      }
      if (poll.status === 'expired') break; // restart the handshake
    }
    // Expired: show a brief note, then loop to mint a fresh code.
    await render('Code expired.\nStarting over...');
    await sleep(1500);
  }
}
