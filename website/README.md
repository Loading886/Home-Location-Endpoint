# Project website

This directory is the source of <https://applelocation.shutiao.us/>.

The production host serves the files directly from `/var/www/applelocation` through nginx. Keep the
site self-contained: no private credentials, remote JavaScript, analytics, or build-time dependencies.

Before deployment:

1. Validate all local links and HTML structure.
2. Render the home page and changed guides at desktop and mobile widths.
3. Back up the current production directory.
4. Stage the new files outside the document root, then install them with directories mode `0755` and
   files mode `0644`.
5. Run `nginx -t` and verify every public page over HTTPS.

The static asset query version in each HTML page must be bumped whenever CSS or JavaScript behavior
changes, so Cloudflare and browsers do not mix old assets with new markup.
