// Clipper engine location.
//
// The UI is static and can be hosted anywhere (Vercel, Netlify, GitHub Pages,
// a plain file server). The Python engine cannot run on those platforms -- it
// needs ffmpeg, OpenCV, faster-whisper and a persistent disk.
//
// So the UI asks where the engine lives. Three ways to set it, in order:
//
//   1. localStorage, set from the gear icon in the top right (per browser)
//   2. window.CLIPPER_API, defined by editing config.js next to index.html
//      (per deployment -- this is what you change on Vercel)
//   3. same origin (the default: engine and UI served together, e.g. localhost)
//
// An empty string means "same origin".
window.CLIPPER_API = window.CLIPPER_API || "";