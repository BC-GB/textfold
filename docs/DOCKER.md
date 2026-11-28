# Run TextFold locally in Docker

Use a current Docker Desktop in Linux container mode, or Docker Engine 28 or later with Docker Compose. The build downloads the pinned official Python image, signed Debian updates, and hash-locked wheels for Linux amd64 and arm64. The default reader runs offline once built. [Docker Desktop on Windows](https://docs.docker.com/desktop/setup/install/windows-install/)

```text
git clone https://github.com/BC-GB/textfold.git
cd textfold
docker compose up --build -d --wait
docker compose exec textfold python -m textfold local-access
```

Open **http://127.0.0.1:3017**, enter `textfold` as the username, and use the session password shown in your terminal. Get a fresh password after restarting the reader. If you set `TEXTFOLD_PORT` before running Compose, use that port in the browser.

Import a sample file from the browser to try the reader. Compose keeps your library and settings in a Docker volume. `docker compose down` stops the containers and retains the volume. The browser gateway publishes only to your computer's loopback address. It shares a socket with the reader and has no library mount. The reader runs as an unprivileged user with a read-only system and no network interface beyond loopback.

To refresh Debian security updates when Docker has cached the build, run `docker compose build --no-cache` followed by `docker compose up -d --wait`.

## Online sources and AI

Use the online override when you want the reader to fetch web sources, use a model, or install an optional PDF engine. It gives the reader outbound network access. Your browser connection and library volume stay the same.

```text
docker compose -f compose.yml -f compose.online.yml up --build -d --wait
docker compose exec textfold python -m textfold local-access
```

Select a provider in Settings and use a sample document when connecting an account for the first time. Selected providers receive the document text needed for your requests. Switch back to the offline setup with `docker compose up -d --wait`.

Docker runs the browser reader. Browser-extension capture, Zotero Desktop, and agents installed on your computer need their own integration with the container.

## Access TextFold through Tailscale Serve

Run Tailscale on the Docker host. Give only your Tailscale user access to HTTPS port 443 on this host. Check the whole tailnet policy for broader grants to the same host. Keep the Compose port published on loopback and block public inbound traffic at your VM firewall. [Tailscale private Docker guide](https://tailscale.com/docs/solutions/host-private-server-docker)

Enable Tailscale Serve on the host and target `http://127.0.0.1:3017`. Save the hostname from its HTTPS URL in the checkout's `.env` file. Replace the example with your host's exact name.

```dotenv
TEXTFOLD_TAILSCALE_HOST=textfold.example.ts.net
```

```sh
docker compose up --build -d --wait
docker compose exec textfold python -m textfold local-access
```

Open the Serve HTTPS URL from a device signed in to your tailnet and use the same TextFold browser credentials. TextFold authorizes only the configured HTTPS host and local loopback hosts. Starting ChatGPT or OpenRouter sign-in requires a local browser session for its OAuth callback.
