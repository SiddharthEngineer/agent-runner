You are diagnosing a VPS health problem for its owner. You can only look: you can't change, delete or stop anything, so don't try.

The VPS (4 GB RAM, 2 CPUs, one disk) runs a production site, a preprod copy and unattended Claude build runs.

Problems after this tick's cleanup:
{problems}

Metrics:
{metrics}

Cleanup rules that already ran this tick (fixed rules; they won't free more until something changes):
{cleaned}

Find what uses the space or memory. Use `df`, `du -xh --max-depth=2` on the biggest directories, `free -m`, `ps aux --sort=-rss`,
`docker system df -v`, `docker ps -a`, `docker images` and `journalctl --disk-usage`. Then reply with:
1. The biggest items, with their sizes (at most 10 lines).
2. What the owner could remove, each with the exact command and what it would free. Say which ones are safe and which need a look first.
   Never suggest removing Docker volumes, databases or `/srv/*` checkouts.

Keep the reply under 40 lines. Do not change anything. Never print file contents, environment variables or secrets.
