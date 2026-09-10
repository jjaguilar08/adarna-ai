# Build & deploy the Adarna landing page

Paste everything below the line into the other Claude Code session — it's
ready to use as-is.

---

Adarna (this repo) is a finished personal project — a real-time AI co-pilot that
listens during a live call, transcribes locally, and surfaces suggested
responses in an always-on-top overlay. I want a real, production-quality
landing page for it on my portfolio site, and I want it actually deployed, not
just built.

## Starting point — read before writing anything

Two things already exist under `design_preferences/` in this repo:

- `landing_page_prompt.md` — the original design brief: what the app is, why
  it's worth showcasing, the tone (portfolio case study, not SaaS marketing),
  and the suggested section layout (hero, how-it-works, feature highlights,
  under-the-hood architecture, tech stack, footer/CTA to GitHub).
- `Adarna - Standalone.html` — a generated visual mockup built from that
  brief. Read it for the actual visual language to match: light background
  (`#f2f2f3`), near-black text (`#1d1f20`), a muted blue-gray accent
  (`#5980a6` family), `Barlow Condensed` uppercase tracked-out headings over
  `Barlow` body text, and small corner-bracket (`⌐ ¬`-style) decorative
  accents on cards — a technical "spec sheet / dossier" feel, not a soft
  SaaS-startup look.

Match that visual language closely. Don't just reuse the mockup file
as-is, though — it's a single bundled/generated HTML export (look for a
"Bundled Page" loading shim near the top), and its hero/feature visuals are
entirely fabricated CSS mockups of the app's UI, not real screenshots. Rebuild
the page as clean, hand-authored static HTML/CSS (vanilla JS only if actually
needed for something like a mobile nav toggle) — no heavy framework, this is
one static page. Reuse the mockup's actual copy and layout structure where
it's good; don't reinvent content that already works.

## The one real requirement: actual screenshots, not mockups

Replace every fabricated UI illustration in the mockup (the hero visual, the
"glance at the overlay" section, any faked window chrome) with real
screenshots of the actual running app. This is the main thing the first pass
got wrong and the main thing to get right this time.

How to get them, using this project's own established WSL→Windows interop
(see `start_app.sh` and `CLAUDE.md` for how this repo already runs
`windows_app`'s real Windows-native Python from WSL):

1. Drive the real `OverlayWindow`, main window, and suggestions window
   classes directly from `windows_app/main.py` with realistic *fabricated*
   sample content (a plausible work-meeting or interview exchange — never
   real personal transcript data; this app's own privacy design never
   persists real sessions to disk, and a portfolio page shouldn't either).
   `wsl_app/research/day30_overlay_smoke_test.py` in this repo is a working
   example of instantiating `OverlayWindow` standalone and calling
   `update_suggestion()` on it via `windows_app/.venv/Scripts/python.exe` —
   follow that pattern for whichever windows need a screenshot.
2. Capture real screenshots via a PowerShell script run the same way (through
   `wsl.exe` interop), using .NET (`System.Drawing.Graphics.CopyFromScreen` or
   the window-handle + `PrintWindow` approach) to grab just the relevant
   window rather than a messy full-desktop capture. Save PNGs into a new
   `design_preferences/screenshots/` (or similar) folder.
3. Get at least: the main window (transcript + suggestions panes), the
   separate suggestions window (added Day 27), and the overlay itself
   showing a good, readable suggestion — the overlay one is probably the
   best hero image, since "glance at a live suggestion" is the actual
   product moment.
4. Before capturing, make sure window titles/chrome show nothing personal
   (plain "Adarna", not a recording-indicator title) and the sample
   suggestion text reads well at a glance — this is showcase content, worth
   iterating on wording once or twice rather than using the first draft.
5. If scripted capture turns out unreliable for some reason, it's fine to ask
   me to manually screenshot a window myself (I can run the real app) rather
   than spending a long time fighting Windows automation for this one step.

## Build

- Plain static HTML/CSS (+ minimal vanilla JS if needed). One page.
- Use the real screenshots from above in place of every mockup visual.
- Keep the section structure from the design brief unless something reads
  better restructured — use judgment, this doesn't need to be a literal
  clone of the mockup.
- Footer keeps a link to the real GitHub repo
  (`github.com/jjaguilar08/adarna-ai`).

## Stop here and let me check it first

Once the page is built, don't move on to the Deploy section on your own.
Serve it locally (e.g. `python3 -m http.server` from the build output
folder) or just tell me the file path to open directly, then stop and wait
for me to actually look at it and say it's good. Only proceed to Deploy
once I've explicitly confirmed that.

## Deploy

Target: a DigitalOcean droplet at `<droplet-ip-redacted>` (root SSH key already set
up in WSL: `ssh root@<droplet-ip-redacted>`) that's already live and already
serving other portfolio projects of mine as subdomains under
`jjaguilar.dev`: `ledger.jjaguilar.dev`, `beacon.jjaguilar.dev`,
`api.beacon.jjaguilar.dev`, `eagleeye.jjaguilar.dev`, plus the bare
`jjaguilar.dev`/`www.jjaguilar.dev` itself. This is another one of those,
not a new domain or a new box: `adarna-ai.jjaguilar.dev`.

Already confirmed directly on the droplet (read-only check, nothing changed
yet), so there's no need to re-discover this — just follow it:

- nginx configs live one-per-site in `/etc/nginx/sites-available/`, symlinked
  into `/etc/nginx/sites-enabled/`. Existing filenames: `ledger`,
  `beacon-app`, `beacon-api`, `eagleeye-landing`, `jjaguilar-dev`.
- `eagleeye.jjaguilar.dev` is the closest match to what this is (a static,
  landing-page-only site with no backend): `root /var/www/eagleeye-landing;
  index index.html;`, `location / { try_files $uri $uri/ =404; }`, a
  dotfile-deny block, and per-site access/error logs. Mirror this file
  almost exactly for the new site — same shape, just swapping the
  server_name and root path.
- Every existing subdomain got its TLS cert individually via
  `certbot --nginx -d <subdomain>` (separate Let's Encrypt cert per
  subdomain, not one shared/wildcard cert) — the nginx file starts as
  HTTP-only (`server_name ...; listen 80;`) and certbot edits it in place to
  add the HTTPS server block and the 80→443 redirect. Don't hand-write the
  HTTPS block yourself first; let certbot do that step, same as the others.
- DNS for every existing subdomain is a plain A record pointing straight at
  `<droplet-ip-redacted>` — confirmed via `dig` (no CNAME, no wildcard). The new
  one needs exactly the same: one A record, host `adarna-ai`, pointing at
  `<droplet-ip-redacted>`. `adarna-ai.jjaguilar.dev` currently resolves to nothing
  (Porkbun's own default), confirming this record doesn't exist yet.

Steps:

1. Create `/var/www/adarna-ai-landing` on the droplet and deploy the built
   static files there via rsync/scp — not by hand-editing files over SSH.
2. Add `/etc/nginx/sites-available/adarna-ai-landing`, modeled directly on
   the existing `eagleeye-landing` file (see above), `server_name
   adarna-ai.jjaguilar.dev;`, `root /var/www/adarna-ai-landing;`. Symlink it
   into `sites-enabled/`, `nginx -t`, then reload nginx — don't restart it
   (existing sites are live).
3. I'll add the DNS record on my end: A record, host `adarna-ai`, answer
   `<droplet-ip-redacted>`, in Porkbun's dashboard for the `jjaguilar.dev` zone.
   Wait for it to actually propagate (`dig +short adarna-ai.jjaguilar.dev
   A` should return `<droplet-ip-redacted>`) before moving on to HTTPS.
4. Once DNS resolves, run `certbot --nginx -d adarna-ai.jjaguilar.dev` —
   same pattern as every existing subdomain. Don't ship this over plain
   HTTP.
5. Verify by actually loading `https://adarna-ai.jjaguilar.dev` in a
   browser (or `curl -I`), not just by trusting the deploy script exited 0
   — and spot-check that `ledger`/`beacon`/`eagleeye`/`jjaguilar.dev` still
   load fine too, since they're all on the same box.

Confirm with me before anything destructive or hard to reverse (touching an
existing site/service's config, restarting nginx instead of reloading it,
DNS changes beyond the one record above, etc.) — this is a real public box
serving other live projects, not a throwaway.
