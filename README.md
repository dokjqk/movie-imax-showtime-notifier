# Shaw Lido IMAX Showtime Watcher

Watches Shaw Theatres' real API directly for new Lido IMAX showtimes and
availability changes, pings [ntfy.sh](https://ntfy.sh) when something
changes, and publishes a status page via GitHub Pages. Runs on a GitHub
Actions schedule -- no local machine needs to stay on. See the docstring at
the top of [watch_shaw_lido.py](watch_shaw_lido.py) for how the API itself
was reverse-engineered.

## One-time setup

1. **Create a GitHub repo** and push this folder to it:
   ```
   git remote add origin https://github.com/<you>/<repo>.git
   git push -u origin main
   ```

2. **Pick an ntfy.sh topic** only you know (topics are unauthenticated --
   anyone who knows the name can read it), e.g. `hendi-shaw-lido-8f2k`.
   Open `https://ntfy.sh/<topic>` in a browser or the ntfy app to watch it.

3. **Add the topic as a repo secret**: repo Settings -> Secrets and
   variables -> Actions -> New repository secret -> name it `NTFY_TOPIC`,
   value = your topic name.

4. **Enable GitHub Pages**: repo Settings -> Pages -> Build and deployment
   -> Source -> **GitHub Actions**. (Not "Deploy from a branch" -- the
   workflow deploys the `docs/` folder itself.)

5. **Trigger the first run manually**: repo Actions tab -> "Watch Shaw
   Lido IMAX" -> Run workflow. After it finishes, your status page is live
   at `https://<you>.github.io/<repo>/`.

After that it runs automatically every 30 minutes, 7am-11pm Singapore time
(`.github/workflows/watch.yml`), commits the updated `seen_state.json` and
`docs/index.html` back to the repo each run, and pushes an ntfy
notification whenever a new Lido IMAX showtime appears or an existing
one's availability status changes.

## Why a page can't just poll Shaw itself

Shaw's API doesn't send CORS headers, so a browser tab on any other origin
(including a plain static page) is blocked from reading the response
client-side. The actual polling has to happen server-side -- that's what
the scheduled GitHub Actions run does -- and the status page just shows
whatever that last run found. It won't update while you're staring at it;
refresh after the next scheduled run (or click "Run workflow" manually).

## Running it locally

```
uv run watch_shaw_lido.py --once -v --topic <your-topic>
```

Useful flags: `--movie "The Odyssey"` to filter by title, `--no-seat-detail`
to skip the slower per-showtime seat-count calls, `--status-page ""` to
skip writing the HTML page. Full flag list: `uv run watch_shaw_lido.py -h`.
