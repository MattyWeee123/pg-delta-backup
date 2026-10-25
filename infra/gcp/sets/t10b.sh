# EXP-28 (T10b, lean): v1 reading a live, busy primary, with the load's client on its own VM. Source and dest as
# EXP-12's (n2-standard-8, local NVMe), the client an n2-standard-4 (config.sh's client role), all in one zone.
ROLES=${ROLES:-source dest client}
ZONE=${ZONE:-us-central1-b}
