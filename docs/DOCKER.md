# Run TextFold locally in Docker

Use a current Docker Desktop in Linux container mode, or Docker Engine 28 or later with Docker Compose. The build uses the official Python image and hash-locked wheels for Linux amd64 and arm64. Building downloads these inputs. The default reader runs offline once built. [Docker Desktop on Windows](https://docs.docker.com/desktop/setup/install/windows-install/)

```text
git clone https://github.com/BC-GB/textfold.git
cd textfold
docker compose up --build -d --wait
docker compose exec textfold python -m textfold local-access
```

Open **http://127.0.0.1:3017**, enter `textfold` as the username, and use the session password shown in your terminal. Get a fresh password after restarting the reader. If you set `TEXTFOLD_PORT` before running Compose, use that port in the browser.

Import a sample file from the browser to try the reader. Compose keeps your library and settings in a Docker volume. `docker compose down` stops the containers and retains the volume. The browser gateway publishes only to your computer's loopback address. It shares a socket with the reader and has no library mount. The reader runs as an unprivileged user with a read-only system and no network interface beyond loopback.

## Online sources and AI

Use the online override when you want the reader to fetch web sources, use a model, or install an optional PDF engine. It gives the reader outbound network access. Your browser connection and library volume stay the same.

```text
docker compose -f compose.yml -f compose.online.yml up --build -d --wait
docker compose exec textfold python -m textfold local-access
```

Select a provider in Settings and use a sample document when connecting an account for the first time. Selected providers receive the document text needed for your requests. Switch back to the offline setup with `docker compose up -d --wait`.

Docker runs the browser reader. Browser-extension capture, Zotero Desktop, and agents installed on your computer need their own integration with the container. Keep the published address on loopback. Access from another device needs a separate private connection that preserves TextFold's authentication.
