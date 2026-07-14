'use client';

// Instructions page shown when /glasses is opened in a normal browser (the
// Even WebView gets the lens app instead — see GlassesApp.tsx detection).
// Sections: account link, Even AI agent setup, glasses app install, meeting
// mode, and the spoken-trigger cheat sheet.
import React from 'react';

const LINK_URL = 'https://www.constella.app/glasses/link';

function Kbd({ children }: { children: React.ReactNode }) {
  return <span style={S.kbd}>{children}</span>;
}

function Step({ n, title, children }: { n: number; title: string; children: React.ReactNode }) {
  return (
    <div style={S.step}>
      <div style={S.stepNum}>{n}</div>
      <div style={{ flex: 1 }}>
        <div style={S.stepTitle}>{title}</div>
        <div style={S.stepBody}>{children}</div>
      </div>
    </div>
  );
}

function Section({
  tag,
  title,
  sub,
  children,
}: {
  tag: string;
  title: string;
  sub?: string;
  children: React.ReactNode;
}) {
  return (
    <section style={S.section}>
      <div style={S.tag}>{tag}</div>
      <h2 style={S.h2}>{title}</h2>
      {sub && <p style={S.sub}>{sub}</p>}
      {children}
    </section>
  );
}

export default function Docs() {
  return (
    <div style={S.page}>
      <div style={S.container}>
        {/* Hero */}
        <header style={S.hero}>
          <div style={S.heroGlyph}>✦</div>
          <h1 style={S.h1}>Constella on Even G2</h1>
          <p style={S.heroSub}>
            Your memory, on your glasses. Capture thoughts, recall anything, and
            transcribe whole meetings — hands-free, straight into Constella.
          </p>
          <div style={S.heroPills}>
            <span style={S.pill}>Voice capture</span>
            <span style={S.pill}>AI recall + integrations</span>
            <span style={S.pill}>Meeting transcription</span>
          </div>
        </header>

        {/* 1. Link account */}
        <Section
          tag='Setup 1'
          title='Link your Constella account'
          sub='One-time, ~1 minute, on your phone.'
        >
          <Step n={1} title='Open the link page'>
            Go to{' '}
            <a style={S.a} href={LINK_URL}>
              constella.app/glasses/link
            </a>{' '}
            in your phone browser.
          </Step>
          <Step n={2} title='Sign in with your Constella email'>
            Enter your email, then the 6-digit code we send you.
          </Step>
          <Step n={3} title='Keep the page open'>
            After verifying, the page shows your <b>Agent URL</b>, <b>Token</b>, and{' '}
            <b>Name</b> — you need them for the next section.
          </Step>
        </Section>

        {/* 2. Agent setup */}
        <Section
          tag='Setup 2'
          title='Hands-free: "Hey Even" agent'
          sub='Capture and recall by voice from anywhere — no app open, phone in pocket.'
        >
          <Step n={1} title='Add Constella as your Even AI agent'>
            Even Realities app → <Kbd>Settings</Kbd> → <Kbd>Even AI</Kbd> →{' '}
            <Kbd>Add Agent</Kbd>. Paste the <b>Name</b>, <b>URL</b>, and <b>Token</b>{' '}
            from the link page, then select Constella as the active agent.
          </Step>
          <Step n={2} title='Capture a thought'>
            Say <Kbd>Hey Even</Kbd>, then start with{' '}
            <b>&ldquo;Remember...&rdquo;</b> or <b>&ldquo;Memo...&rdquo;</b> —
            it&apos;s saved to your memory instantly, with a clean title.
          </Step>
          <Step n={3} title='Recall anything'>
            Say <Kbd>Hey Even</Kbd>, then <b>&ldquo;Recall...&rdquo;</b>,{' '}
            <b>&ldquo;Lookup...&rdquo;</b>, or{' '}
            <b>&ldquo;What do I know about...&rdquo;</b>. The answer is composed by
            an AI agent with access to your whole memory and connected apps
            (calendar, email, tasks) — and follow-up questions keep context.
          </Step>
          <div style={S.callout}>
            <b>Phrasing matters:</b> avoid opening with <i>save / list / add / note</i>{' '}
            on the &ldquo;Hey Even&rdquo; path — Even&apos;s built-in QuickList
            intercepts those before Constella sees them. &ldquo;Remember&rdquo; and
            &ldquo;Recall&rdquo; always reach Constella.
          </div>
        </Section>

        {/* 3. Install the app */}
        <Section
          tag='Setup 3'
          title='Put the Constella app on your glasses'
          sub='Needed for Meeting mode (continuous transcription requires the app).'
        >
          <Step n={1} title='Load it into the Even app'>
            With Developer Mode on, scan a QR pointing at{' '}
            <code style={S.code}>https://www.constella.app/glasses</code> (generate
            one with{' '}
            <code style={S.code}>
              npx @evenrealities/evenhub-cli qr --url &quot;https://www.constella.app/glasses&quot;
            </code>
            ), or install the packaged app through the dev portal for a permanent
            entry.
          </Step>
          <Step n={2} title='Launch from the glasses'>
            Long-press the temple <Kbd>TouchBar</Kbd> → <Kbd>Menu</Kbd> → swipe to{' '}
            <Kbd>Constella</Kbd> → tap. No phone in hand needed.
          </Step>
          <Step n={3} title='Quick mode: tap, speak, done'>
            Tap → speak → stop talking. It auto-detects silence, then answers your
            question or saves your thought — same brain as the agent.
          </Step>
        </Section>

        {/* 4. Meeting mode */}
        <Section
          tag='Meeting mode'
          title='Transcribe whole meetings into memory'
          sub='Continuous transcription, saved as one searchable note the moment you finish.'
        >
          <Step n={1} title='Enter MEETING mode'>
            In the Constella app, <b>swipe</b> the TouchBar to switch to MEETING mode
            (the app remembers your last mode on next launch).
          </Step>
          <Step n={2} title='Tap to start'>
            The lens shows elapsed time and the live transcript tail. Audio uploads
            in the background every 25 seconds — dropped connections retry, nothing
            is lost.
          </Step>
          <Step n={3} title='Live AI cues while you talk'>
            As the conversation flows, Constella quietly analyzes it, searches your
            memories (and their connections), checks the web when useful, and pushes
            one sharp insight to the lens — a fact you saved, a related note, a
            question worth asking. Cues appear on their own; no interaction needed.
          </Step>
          <Step n={4} title='Tap to finish'>
            The full transcript is saved to Constella as one note (chunked and
            indexed like every other memory). Recall it seconds later:{' '}
            <i>&ldquo;Hey Even... recall what we decided in today&apos;s standup.&rdquo;</i>
          </Step>
          <div style={S.callout}>
            Keep the Even app foregrounded on your phone during meetings, and expect
            up to 3 hours per session. Tip: launch straight into recording with{' '}
            <code style={S.code}>constella.app/glasses?mode=meeting&amp;autostart=1</code>{' '}
            as a dedicated QR entry.
          </div>
        </Section>

        {/* 5. Cheat sheet */}
        <Section tag='Reference' title='Cheat sheet'>
          <div style={S.tablesWrap}>
            <div style={S.tableCol}>
              <div style={S.tableTitle}>Say it (&ldquo;Hey Even&rdquo; or Quick mode)</div>
              <table style={S.table}>
                <tbody>
                  <tr>
                    <td style={S.tdKey}>Remember... / Memo...</td>
                    <td style={S.td}>saves a note</td>
                  </tr>
                  <tr>
                    <td style={S.tdKey}>Note to self... / Don&apos;t forget...</td>
                    <td style={S.td}>saves a note</td>
                  </tr>
                  <tr>
                    <td style={S.tdKey}>Recall... / Lookup...</td>
                    <td style={S.td}>searches your memory</td>
                  </tr>
                  <tr>
                    <td style={S.tdKey}>What do I know about...</td>
                    <td style={S.td}>searches your memory</td>
                  </tr>
                  <tr>
                    <td style={S.tdKey}>Anything else</td>
                    <td style={S.td}>auto-routed (defaults to saving)</td>
                  </tr>
                </tbody>
              </table>
            </div>
            <div style={S.tableCol}>
              <div style={S.tableTitle}>Touch (in the Constella app)</div>
              <table style={S.table}>
                <tbody>
                  <tr>
                    <td style={S.tdKey}>Tap</td>
                    <td style={S.td}>speak / stop / start &amp; finish meeting</td>
                  </tr>
                  <tr>
                    <td style={S.tdKey}>Swipe up / down</td>
                    <td style={S.td}>switch Quick ⇄ Meeting mode</td>
                  </tr>
                  <tr>
                    <td style={S.tdKey}>Double-tap</td>
                    <td style={S.td}>exit (in a meeting: finish &amp; save)</td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>
        </Section>

        <footer style={S.footer}>
          Constella · your permanent memory ·{' '}
          <a style={S.a} href='https://www.constella.app'>
            constella.app
          </a>
        </footer>
      </div>
    </div>
  );
}

const S: Record<string, React.CSSProperties> = {
  page: {
    minHeight: '100vh',
    background: '#0b0d12',
    color: '#e9ecf1',
    fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif',
    padding: '48px 20px 64px',
  },
  container: { maxWidth: 760, margin: '0 auto' },
  hero: { textAlign: 'center', marginBottom: 48 },
  heroGlyph: { fontSize: 40, color: '#8fd48f', marginBottom: 12 },
  h1: { fontSize: 34, margin: '0 0 12px', letterSpacing: -0.5 },
  heroSub: {
    fontSize: 17,
    lineHeight: 1.55,
    color: '#99a2b2',
    maxWidth: 560,
    margin: '0 auto 20px',
  },
  heroPills: { display: 'flex', gap: 8, justifyContent: 'center', flexWrap: 'wrap' },
  pill: {
    fontSize: 13,
    padding: '6px 14px',
    borderRadius: 999,
    border: '1px solid #2d333f',
    background: '#151922',
    color: '#b7bfcc',
  },
  section: {
    background: '#12151d',
    border: '1px solid #232936',
    borderRadius: 18,
    padding: '28px 26px',
    marginBottom: 24,
  },
  tag: {
    display: 'inline-block',
    fontSize: 12,
    fontWeight: 700,
    letterSpacing: 1,
    textTransform: 'uppercase',
    color: '#8fd48f',
    marginBottom: 8,
  },
  h2: { fontSize: 22, margin: '0 0 6px' },
  sub: { margin: '0 0 20px', color: '#99a2b2', fontSize: 15, lineHeight: 1.5 },
  step: { display: 'flex', gap: 14, marginBottom: 18 },
  stepNum: {
    width: 26,
    height: 26,
    borderRadius: 999,
    background: '#1d2430',
    border: '1px solid #2d333f',
    color: '#8fd48f',
    fontSize: 13,
    fontWeight: 700,
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    flexShrink: 0,
    marginTop: 2,
  },
  stepTitle: { fontSize: 15.5, fontWeight: 600, marginBottom: 4 },
  stepBody: { fontSize: 14.5, lineHeight: 1.6, color: '#b7bfcc' },
  callout: {
    marginTop: 6,
    padding: '12px 16px',
    borderRadius: 12,
    background: '#161c14',
    border: '1px solid #2b3a26',
    fontSize: 14,
    lineHeight: 1.6,
    color: '#c4d2be',
  },
  kbd: {
    display: 'inline-block',
    padding: '1px 8px',
    borderRadius: 6,
    border: '1px solid #2d333f',
    background: '#0f1216',
    fontSize: 13,
    color: '#e9ecf1',
  },
  code: {
    padding: '1px 6px',
    borderRadius: 6,
    background: '#0f1216',
    border: '1px solid #2d333f',
    fontSize: 12.5,
    color: '#b7bfcc',
    overflowWrap: 'anywhere',
  },
  a: { color: '#8fd48f', textDecoration: 'none' },
  tablesWrap: { display: 'flex', gap: 20, flexWrap: 'wrap' },
  tableCol: { flex: '1 1 300px', minWidth: 280 },
  tableTitle: { fontSize: 13.5, fontWeight: 700, color: '#99a2b2', margin: '8px 0' },
  table: { width: '100%', borderCollapse: 'collapse', fontSize: 14 },
  tdKey: {
    padding: '8px 10px',
    border: '1px solid #232936',
    color: '#e9ecf1',
    fontWeight: 600,
    width: '52%',
  },
  td: { padding: '8px 10px', border: '1px solid #232936', color: '#b7bfcc' },
  footer: { textAlign: 'center', marginTop: 40, color: '#5d6675', fontSize: 13.5 },
};
