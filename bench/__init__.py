"""bench: run a benchmark on a laptop or on Google Cloud, record everything, and publish it as a pull request.

    python -m bench run --where laptop --publish
    python -m bench run --where gcp --preset quick --link 1gbit --publish
    python -m bench estimate --where gcp --preset baseline
    python -m bench cleanup

Every run lands in runs/<name>/: the config, one event file per lane (watchable live with python -m viz),
the 0.1-second samples of cloud VMs, the statistics, report.md and charts. See bench/README.md.
"""
