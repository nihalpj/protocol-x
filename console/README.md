# Protocol-X Tool MCP — console SPA

React 19 + Vite + Tailwind + shadcn/ui single-page console for the Protocol-X
Tool MCP server. Talks to the Python server's same-origin `/api/*` REST routes;
the built bundle is served by the server from `console/dist` (works on
air-gapped hosts — no CDN or external font references).

## Scripts

- `npm ci` — reproducible install
- `npm run dev` — Vite dev server (API calls need the Python server running, see below)
- `npm run build` — production build to `dist/`
- `npm run check` — `tsc -b` type check
- `npm run lint` — ESLint
- `npm run preview` — preview the built bundle

## Dev against a live server

The dev server proxies nothing: the console expects `/api/*` on the same
origin. For development either build and let the Python server serve `dist/`,
or run Vite with a proxy, e.g.:

```sh
npx vite --port 5173 --strictPort \
  --server.proxy./api.target=http://localhost:7894
```

## Layout

- `src/api.ts` — typed fetch client for `/api/status|tools|logs|config|governance|pypi|snippets` plus a small polling hook
- `src/components/console/` — HeaderBar, Registry, Telemetry, Governance, SnippetsPage, AddPackageModal, SettingsModal
- `src/pages/ConsolePage.tsx` — tab shell (Registry · Telemetry · Governance · Snippets)
