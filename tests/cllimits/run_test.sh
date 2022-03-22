#!/bin/bash

set -x

if ! cat /etc/os-release | grep CloudLinux; then
    echo "This test is only for CloudLinux"
    echo "Test SUCCESS"
    exit 0
fi

USERS_COUNT=6
USERNAME_PREFIX="cluser"
USER1="${USERNAME_PREFIX}1"
TIMEOUT="1m"

function create_users() {
    rc=0
    for user in $(seq 1 $USERS_COUNT); do
        username="${USERNAME_PREFIX}${user}"
        useradd "${username}" || rc=$(( rc + 1 ))
    done
    cagefsctl --enable "${USER1}"
    return $rc
}

function delete_users() {
    cagefsctl --disable "${USER1}"
    cagefsctl --unmount "${USER1}"
    for user in $(seq 1 $USERS_COUNT); do
        username="${USERNAME_PREFIX}${user}"
        userdel -rf "${username}"
    done
}

function test_cagefs() {
    user1_passwd=$(su -c "cat /etc/passwd" - "${USER1}")
    echo "${user1_passwd}" | grep -P "user[2-9]"
    grep_rc=$?
    if [[ $grep_rc == 0 ]]; then
        echo "Failed, other user is visible in /etc/passwd"
        return 1
    fi

    echo "Success, CageFS is working properly"
    return 0
}

function test_lve_nproc() {
    user_nproc=cluser2
    user_nproc_id="$(id -u "${user_nproc}")"
    nproc_limit=20
    lvectl set "${user_nproc_id}" --ep=5 --nproc=${nproc_limit}
    lvectl apply all

    su -c "for i in {1..30} ; do sleep 100 & done" - "${user_nproc}"
    process_spawn=$?
    if [[ $process_spawn == 0 ]]; then
        echo "Failed, processes were spawn"
        return 1
    fi
    users_process_count=$(ps -u "${user_nproc}" | wc -l)
    killall -u "${user_nproc}"
    if (( $users_process_count > $nproc_limit )); then
        echo "Failed, count of user's processes (${users_process_count}) is greater than the limit ${nproc_limit}"
        return 1
    fi
    echo "Success, Nproc limitation is working properly"
    return 0
}

function test_lve_cpu() {
    user_cpu=cluser3
    user_cpu_id="$(id -u ${user_cpu})"
    cpu_limit=10
    lvectl set "${user_cpu_id}" --speed="${cpu_limit}%"
    lvectl apply all

    su -c "stress-ng --cpu 0 --cpu-method all --timeout ${TIMEOUT} &" - "${user_cpu}"
    stress_rc=$?

    if [[ "${stress_rc}" != 0 ]]; then
        echo "Failed, stress-ng not found"
        return 1
    fi

    usage=$(lveps -c 60 -d -o "id:10,cpu:10" | grep "${user_cpu}")

    killall -u "${user_cpu}"

    if [[ -z "${usage}" ]]; then
        echo "Failed, lveps parsing is broken, usage is empty!"
        return 1
    fi
    cpu_usage=$(echo "${usage}" | awk -F '[[:space:]]+' '{print $3}')
    raw_usage=$(echo "${cpu_usage}" | tr -d '%')
    echo "${raw_usage}" | grep -qvP '[a-zA-Z%]'
    grep_rc=$?

    if [[ "${grep_rc}" != 0 ]]; then
        echo "Failed, lveps parsing is broken, usage: '${raw_usage}'"
        return 1
    fi
    if (( raw_usage > cpu_limit )); then
        echo "Failed, cpu usage is greater than limit"
        return 1
    fi
    echo "Success, CPU limitation is working properly"
    return 0
}

function test_lve_pmem() {
    user_pmem=cluser4
    user_pmem_id="$(id -u "${user_pmem}")"
    pmem_limit=512
    lvectl set "${user_pmem_id}" --pmem=${pmem_limit}
    lvectl apply all

    # /bin/bash size is 944 KB
    su -c "cp /bin/bash /dev/shm/bash" - "${user_pmem}"
    cp_rc=$?
    if [[ $cp_rc == 0 ]]; then
        echo "Failed, cp to memory was successful, but shouldn't"
        return 1
    fi
    echo "Success, PMEM limitation is working properly"
    return 0
}

function test_lve_io() {
    user_io=cluser5
    user_io_id="$(id -u "${user_io}")"
    io_limit=1
    # pmem/vmem/speed are unlimited to be able to use fio
    lvectl set "${user_io_id}" --io=${io_limit} --pmem=0 --vmem=0 --speed=100%
    lvectl apply all

    io_test=$(su -c "fio --randrepeat=1 --ioengine=sync --direct=1 --gtod_reduce=1 --name=test --filename=test --bs=4k --iodepth=64 --size=8KB --readwrite=randrw --rwmixread=75" - "${user_io}")
    io_usage=$(echo "${io_test}" | grep -P '(?<=read: IOPS=)\d+(\.\d*)?k?' -o)
    if [[ -z "${io_usage}" ]]; then
        echo "Failed, io parsing is broken: io usage is empty"
        return 1
    fi
    echo "${io_usage}" | grep -qvP '[a-zA-Z%]'
    if [[ "${grep_rc}" != 0 ]]; then
        echo "Failed, io parsing is broken: '${io_usage}'"
        return 1
    fi
    if (( io_usage > io_limit )); then
        echo "Failed, io usage is over limit"
        return 1
    fi
    echo "Success, IO limitation is working properly"
    return 0
}

function test_lve_iops() {
    user_iops=cluser6
    user_iops_id="$(id -u "${user_iops}")"
    iops_limit=250
    # pmem/vmem/speed are unlimited to be able to use fio
    lvectl set "${user_iops_id}" --iops=${iops_limit} --pmem=0 --vmem=0 --speed=100%
    lvectl apply all

    iops_test=$(su -c "fio --randrepeat=1 --ioengine=libaio --direct=1 --gtod_reduce=1 --name=test --filename=test --bs=4k --iodepth=64 --size=100MB --readwrite=randrw --rwmixread=75" - "${user_iops}")
    iops_usage=$(echo "${iops_test}" | grep -P '(?<=read: IOPS=)\d+(\.\d*)?k?' -o)
    if [[ -z "${iops_usage}" ]]; then
        echo "Failed, iops parsing is broken: iops usage is empty"
        return 1
    fi
    echo "${iops_usage}" | grep -qvP '[a-zA-Z%]'
    if [[ "${grep_rc}" != 0 ]]; then
        echo "Failed, iops parsing is broken: '${iops_usage}'"
        return 1
    fi
    if (( iops_usage > iops_limit )); then
        echo "Failed, iops usage is over limit"
        return 1
    fi
    echo "Success, IOPS limitation is working properly"
    return 0
}

summary="Test results:"

function run() {
    local name="$1"
    local method="$2"

    ${method}
    test_exit_code=$?

    status=""
    if [[ $test_exit_code != 0 ]]; then
        status="[fail]"
    else
        status="[ ok ]"
    fi
    summary="${summary}\n${status} ${name}"

    return $test_exit_code
}


exit_code=0

create_users || exit_code=$(( exit_code + 1 ))
if [[ ${exit_code} -eq 0 ]]; then
    echo "++++++++++++++++++++++++++++++++"
    run "cagefs" test_cagefs || exit_code=$(( exit_code + 1 ))
    echo "++++++++++++++++++++++++++++++++"
    run "nproc" test_lve_nproc || exit_code=$(( exit_code + 1 ))
    echo "++++++++++++++++++++++++++++++++"
    run "cpu" test_lve_cpu || exit_code=$(( exit_code + 1 ))
    echo "++++++++++++++++++++++++++++++++"
    run "pmem" test_lve_pmem || exit_code=$(( exit_code + 1 ))
    echo "++++++++++++++++++++++++++++++++"
    run "io" test_lve_io || exit_code=$(( exit_code + 1 ))
    echo "++++++++++++++++++++++++++++++++"
    run "iops" test_lve_iops || exit_code=$(( exit_code + 1 ))
    echo "++++++++++++++++++++++++++++++++"
fi
delete_users

echo -e "${summary}"

if [[ $exit_code != 0 ]]; then
    echo "Test FAILED"
else
    echo "Test SUCCESS"
fi

exit ${exit_code}
