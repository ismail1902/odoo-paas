# Contributing

Thanks for considering a contribution. This project is intended to be easy to
fork and extend while keeping attribution to the original project.

## License of contributions

By submitting a pull request, patch, or other contribution, you agree that your
contribution is licensed under the Apache License, Version 2.0, and that you
have the right to submit it under those terms.

Commits must be signed off under the
[Developer Certificate of Origin 1.1](https://developercertificate.org/):

```bash
git commit -s -m "Describe the change"
```

There is no separate CLA. Contributors retain copyright in their own work;
the project's `LICENSE` and `NOTICE` files must be preserved in redistributions.
If you add substantial original code and want a copyright notice for it, add a
separate notice without removing the existing attribution.

## Development

There is no automated test suite. Before opening a pull request:

```bash
MOCK_DOCKER=1 .venv/bin/uvicorn app.main:app --reload
# exercise the UI you touched: login, projects, branches, settings, builds
.venv/bin/python -m compileall app
node --check app/static/js/dashboard.js
bash -n install.sh
bash -n uninstall.sh
```

Read [AGENTS.md](AGENTS.md) for the architecture and invariants. In particular:

- all Docker access belongs in `app/services/docker_ctl.py`;
- all settings belong in `app/config.py`;
- do not copy Odoo Enterprise source or other proprietary code into the repo;
- keep `install.sh`/`uninstall.sh` idempotent and `uninstall.sh --dry-run`
  side-effect free.

## Forking and redistribution

You may fork and redistribute the platform under Apache-2.0. Keep the
`LICENSE` and `NOTICE` files, preserve copyright notices, and mark files you
have changed. Do not use the Odoo name or logo to imply that a fork is an
official Odoo product. Odoo Community and Enterprise have their own licenses;
this repository's license does not grant rights to them.

## Security reports

Please do not publish database credentials, tokens, customer data, or exploit
details in a public issue. Use GitHub's private security advisory feature for
this repository, or contact the maintainer privately through the repository.
