# Security

Please report security problems privately through GitHub: open the repository's **Security** tab and choose
**Report a vulnerability**. Don't open a public issue for them.

What Simple Scope does with data:
- Crawl data, reports and any authorized-access headers or logins are stored only on the computer running it
  (`~/.simplescope/`, or the folder set in `SIMPLESCOPE_HOME`).
- Credentials for authorized access are sent only to the website being audited.
- The app sends no telemetry. Streamlit's usage statistics are switched off.
- The app has no login. Run it on `localhost` (the default); `--host 0.0.0.0` makes it reachable by anyone on the network.
