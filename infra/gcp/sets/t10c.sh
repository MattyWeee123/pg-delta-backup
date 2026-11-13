# EXP-39 (T10b's larger source, lean): EXP-28's design with a 16-vCPU source. Source n2-standard-16, dest
# n2-standard-8 (both local NVMe, as EXP-28's), client n2-standard-4: 28 vCPUs, within the project's 32.
ROLES=${ROLES:-source dest client}
ZONE=${ZONE:-us-central1-b}
SOURCE_MACHINE=${SOURCE_MACHINE:-n2-standard-16}
