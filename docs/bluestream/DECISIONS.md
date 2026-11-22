# DECISIONS
1. Studio is a new *source* that publishes to the same private local RTMP bus Relay uses, so existing publishers, destinations, HLS and BindsTo= ownership work unchanged (no second publish path -> no dual-publisher risk by construction).
2. Pure-Python core (`webapp/studio/`) with no new dependencies; stdlib `zoneinfo` for timezones (needs system tzdata on Linux).
3. Dynamic text via FFmpeg `drawtext textfile=...:reload=1` fed by atomic file replace: no browser for timers/tickers; user text never reaches argv.
4. Layout in fractions of canvas (resolution independent); V1 profiles 720p30 (1080p allowed in model, unbenchmarked).
5. Planner never assumes copy-compatibility from unknown facts (unknown GOP => not copyable).
