# HammerDB TPROC-C schema build for the benchmark fixture.
#
# TPROC-C is HammerDB's TPC-C derived workload. Results from it are NOT
# audited TPC-C results and must never be published as "tpmC".
#
# Run with:
#   docker run --rm --network fixture_default \
#     -e PGBENCH_HOST=postgres -e PGBENCH_PASS="$POSTGRES_PASSWORD" \
#     -e TPCC_WAREHOUSES=10 -e TPCC_BUILD_VU=8 \
#     -v "$PWD/benchmarks/workloads/hammerdb":/hdb:ro \
#     tpcorg/hammerdb:latest ./hammerdbcli auto /hdb/build_schema.tcl
#
# 10 warehouses produces roughly a 1 GiB cluster, the "small" scale in
# benchmarks/README.md. Each additional warehouse adds roughly 100 MB.

proc envdefault {name default} {
    if { [info exists ::env($name)] } {
        return $::env($name)
    }
    return $default
}

set pghost      [ envdefault PGBENCH_HOST postgres ]
set pgport      [ envdefault PGBENCH_PORT 5432 ]
set pgpass      [ envdefault PGBENCH_PASS "" ]
set warehouses  [ envdefault TPCC_WAREHOUSES 10 ]
set buildvu     [ envdefault TPCC_BUILD_VU 8 ]

if { $pgpass eq "" } {
    error "PGBENCH_PASS is not set; refusing to build with an empty superuser password"
}

puts "SETTING CONFIGURATION host=$pghost warehouses=$warehouses build_vu=$buildvu"
dbset db pg
dbset bm TPC-C

diset connection pg_host $pghost
diset connection pg_port $pgport
diset connection pg_sslmode prefer

diset tpcc pg_count_ware $warehouses
diset tpcc pg_num_vu $buildvu
diset tpcc pg_superuser postgres
diset tpcc pg_superuserpass $pgpass
diset tpcc pg_defaultdbase postgres
diset tpcc pg_user tpcc
diset tpcc pg_pass tpcc
diset tpcc pg_dbase tpcc
diset tpcc pg_tspace pg_default
diset tpcc pg_storedprocs true

# HammerDB's own guidance partitions the order tables only at larger scales.
if { $warehouses >= 200 } {
    diset tpcc pg_partition true
} else {
    diset tpcc pg_partition false
}

puts "SCHEMA BUILD STARTED"
buildschema
puts "SCHEMA BUILD COMPLETED"
