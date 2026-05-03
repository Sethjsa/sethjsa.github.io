// Last.fm recent tracks widget
// Get a free API key at https://www.last.fm/api/account/create
// then replace the placeholder below and commit.
const LASTFM_API_KEY = "YOUR_API_KEY_HERE";
const LASTFM_USER = "SetheryJ";
const LIMIT = 10;

async function loadLastFm() {
  const el = document.getElementById("lastfm-tracks");
  if (!el) return;
  if (LASTFM_API_KEY === "YOUR_API_KEY_HERE") {
    el.innerHTML = '<li><em>Last.fm API key not set — see js/lastfm.js</em></li>';
    return;
  }
  const url =
    `https://ws.audioscrobbler.com/2.0/?method=user.getrecenttracks` +
    `&user=${LASTFM_USER}&api_key=${LASTFM_API_KEY}&format=json&limit=${LIMIT}`;
  try {
    const res = await fetch(url);
    const data = await res.json();
    const tracks = data.recenttracks.track;
    el.innerHTML = tracks.map((t, i) => {
      const nowPlaying = t["@attr"] && t["@attr"].nowplaying === "true";
      const artist = t.artist["#text"];
      const name = t.name;
      const album = t.album["#text"];
      return `<li class="${nowPlaying ? "nowplaying" : ""}">
        <strong>${name}</strong> &mdash; ${artist}${album ? ` <span style="color:#888">(${album})</span>` : ""}
      </li>`;
    }).join("");
  } catch (e) {
    el.innerHTML = "<li><em>Could not load Last.fm data.</em></li>";
  }
}

document.addEventListener("DOMContentLoaded", loadLastFm);
