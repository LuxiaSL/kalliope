# kalliope — the station exists (SPEC §4, weekend 1)
# shuffle from catalog via picker API → crossfade → normalize → HLS + harbor mp3

settings.log.stdout := true
settings.log.level := 3

api = environment.get("KALLIOPE_API", default="http://127.0.0.1:8321")
hls_dir = environment.get("KALLIOPE_HLS_DIR", default="#{environment.get('HOME')}/.local/state/kalliope/hls")
harbor_port = int_of_string(environment.get("KALLIOPE_HARBOR_PORT", default="8322"))

# --- next track from the picker -------------------------------------------
def next_track() =
  answer = list.hd(default="",
    process.read.lines("curl -sf --max-time 5 #{api}/next"))
  if answer == "" then
    log.important(label="kalliope", "picker unavailable or empty — retrying")
    null
  else
    request.create(answer)
  end
end

radio = request.dynamic(id="rotation", retry_delay=2., next_track)

# --- tell the server what actually hit the air ----------------------------
def notify_aired(m) =
  fname = m["filename"]
  if fname != "" then
    payload = json.object()
    payload.add("filename", fname)
    payload.add("title", m["title"])
    payload.add("artist", m["artist"])
    try
      ignore(http.post(data=json.stringify(payload), timeout=5.,
        headers=[("Content-Type", "application/json")], "#{api}/aired"))
    catch err do
      log.important(label="kalliope", "failed to notify /aired: #{err}")
    end
  end
end

radio.on_track(synchronous=false, notify_aired)

# --- sound ----------------------------------------------------------------
# v1 loudness handling: gain-rider normalization as the NULL-safe fallback
# until track_analysis.loudness_lufs exists (SPEC §1.2)
radio = normalize(radio)
radio = crossfade(fade_in=1., fade_out=2., duration=3., radio)

# the station never stops: silence beats a dead process (proper
# shuffle-from-catalog fallback arrives with the DJ split, SPEC §1.2)
radio = mksafe(radio)

# --- patina ---------------------------------------------------------------
# flavored signal degradation: a whisper of broadband hiss under everything,
# plus a gentle bandpass for worn-transmitter warmth. Sits after mksafe on
# purpose — dead air carries faint static, so the carrier always sounds live.
patina = float_of_string(default=0.002,
  environment.get("KALLIOPE_PATINA", default="0.002"))
warmth = environment.get("KALLIOPE_WARMTH", default="on")

radio =
  if warmth == "on" then
    radio = filter.iir.butterworth.high(frequency=48., order=2, radio)
    filter.iir.butterworth.low(frequency=11500., order=2, radio)
  else
    radio
  end

radio =
  if patina > 0. then
    hiss = amplify(patina, noise())
    hiss = filter.iir.butterworth.low(frequency=6000., order=2, hiss)
    add(normalize=false, [radio, hiss])
  else
    radio
  end

# --- outputs --------------------------------------------------------------
def station_event(type, who) =
  payload = json.object()
  payload.add("type", type)
  payload.add("who", who)
  try
    ignore(http.post(data=json.stringify(payload), timeout=5.,
      headers=[("Content-Type", "application/json")], "#{api}/event"))
  catch err do
    log.important(label="kalliope", "failed to post #{type}: #{err}")
  end
end

# native mp3 stream for <audio> tags (web player v1); harbor connects are
# the tune-in/tune-out signal — presence costs the client nothing
mp3_out = output.harbor(%mp3(bitrate=192), mount="stream", port=harbor_port, radio)
mp3_out.on_connect(synchronous=false, fun (c) -> station_event("tune_in", c.ip))
mp3_out.on_disconnect(synchronous=false, fun (who) -> station_event("tune_out", who))

# HLS (spec-canonical transport for the Android app later)
aac_stream = %ffmpeg(format="mpegts", %audio(codec="aac", b="160k"))
output.file.hls(playlist="live.m3u8", segment_duration=4.,
  segments=8, persist_at="#{hls_dir}/state.json", hls_dir,
  [("aac_160", aac_stream)], radio)
