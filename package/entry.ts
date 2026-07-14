// Packaged (.ehpk) entrypoint. Unlike the hosted /glasses route there is no
// environment detection: inside the Even app's WebView we ARE the glasses app,
// so boot the lens state machine directly. Store review requires the bundle
// itself to call the bridge APIs (createStartUpPageContainer / onEvenHubEvent /
// shutDownPageContainer), which startGlassesApp does via lib/glasses.ts.
import { startGlassesApp } from '../src/app/glasses/lib/app';

void startGlassesApp();
