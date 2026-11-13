"""viz: watch refresh runs as they happen, or replay recorded ones, in a browser.

    python -m viz serve                      # open http://localhost:8765 and pick a run
    python -m viz race                       # a laptop race: pg_basebackup vs pgdelta, watched live
    python -m viz import-sampler NAME DIR:LABEL ...   # turn recorded VM runs (0.1 s samples) into a replay

A run is a directory with meta.json and one events-<lane>.jsonl per lane (see pgdelta/events.py for the
event format). Runs live in runs/ (not committed) and viz/examples/ (committed).
"""
