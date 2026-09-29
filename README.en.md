# Performer

[Türkçe](README.md) · **English**

Performer measures CPU use, waiting, and lock contention in Linux processes.
It writes a portable `.tgz` bundle; the offline viewer opens bundles in the
browser and compares two runs.

## Viewer

```sh
docker compose up -d --build
```

Open [http://127.0.0.1:8080](http://127.0.0.1:8080) and select a run bundle.
The root [Compose file](compose.yaml) runs only the web viewer. Bundles are read
in the browser and are not uploaded to a server.

## Collection

The Linux target needs Python 3.8+, `bpftrace`, and eBPF privileges:

```sh
sudo ./collector/bin/performer collect --pid PID --duration 30 --profile standard --label baseline --out runs
```

Preflight stops before collection when a required tool or probe file is missing.
If some probes work, failures in the others are recorded as warnings.
GitHub and GitLab CI produce `performer-collector` `.deb` files for Ubuntu
22.04 and 24.04; the installed command is `performer`.

Use `make docker-demo` to test against the sample target, or `make docker-fake`
to generate a sample bundle without eBPF. Their Compose file is
[tests/compose.yaml](tests/compose.yaml).

## Details

- [Detailed technical guide](docs/guide.en.md)
- [Docker demo and requirements](docs/docker-demo.md)
- [Bundle format](docs/bundle-format.md)
- [Viewer development notes](viewer/README.md)
