# HammerDB TPROC-C timed run against the benchmark fixture.
#
# Produces NOPM (new orders per minute) and TPM. These are TPROC-C figures, not
# audited TPC-C results.
#
# The transaction counter prints a TPM sample roughly every 10 seconds. Read
# those samples with "docker logs -t" to get a timestamped series: the aggregate
# NOPM dilutes a short backup across the whole measurement window, so the series
# is what shows the actual impact. See benchmarks/workloads/hammerdb/README.md.
#
# Run with:
#   docker run -d --name hdb-run --network fixture_default \
#     -e PGBENCH_HOST=postgres -e PGBENCH_PASS="$POSTGRES_PASSWORD" \
#     -e TPCC_VU=8 -e TPCC_RAMPUP=1 -e TPCC_DURATION=2 \
#     -v "$PWD/benchmarks/workloads/hammerdb":/hdb:ro \
#     tpcorg/hammerdb:latest ./hammerdbcli auto /hdb/run_timed.tcl

proc envdefault {name default} {
    if { [info exists ::env($name)] } {
        return $::env($name)
    }
    return $default
}

set pghost   [ envdefault PGBENCH_HOST postgres ]
set pgport   [ envdefault PGBENCH_PORT 5432 ]
set pgpass   [ envdefault PGBENCH_PASS "" ]
set vu       [ envdefault TPCC_VU 8 ]
set rampup   [ envdefault TPCC_RAMPUP 1 ]
set duration [ envdefault TPCC_DURATION 2 ]
set dovacuum [ envdefault TPCC_VACUUM false ]

if { $pgpass eq "" } {
    error "PGBENCH_PASS is not set; refusing to run with an empty superuser password"
}

puts "SETTING CONFIGURATION host=$pghost vu=$vu rampup=${rampup}m duration=${duration}m"
dbset db pg
dbset bm TPC-C

diset connection pg_host $pghost
diset connection pg_port $pgport
diset connection pg_sslmode prefer

diset tpcc pg_superuser postgres
diset tpcc pg_superuserpass $pgpass
diset tpcc pg_defaultdbase postgres
diset tpcc pg_user tpcc
diset tpcc pg_pass tpcc
diset tpcc pg_dbase tpcc
diset tpcc pg_driver timed
diset tpcc pg_rampup $rampup
diset tpcc pg_duration $duration
diset tpcc pg_vacuum $dovacuum
diset tpcc pg_timeprofile true
diset tpcc pg_allwarehouse true

loadscript
puts "TEST STARTED"
vuset vu $vu
vucreate
tcstart
set jobid [ vurun ]
vudestroy
tcstop
puts "TEST COMPLETE jobid=$jobid"
