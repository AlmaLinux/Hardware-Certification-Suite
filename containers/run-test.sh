#!/usr/bin/env bash

set -x

prepare() {
    echo "Installing docker..."
    yum install docker -y 1>&2

    which docker >> /dev/null
    if [[ ! $? -eq 0 ]]; then
        echo "Docker installation failed, aborting test"
        return 1
    fi

    return 0
}

teardown() {
    echo "Removing docker..."
    yum remove docker -y 1>&2
}

wait_url_available() {
    attempts=5
    is_alternative="$1"
    command="$2"
    while [[ ${attempts} -gt 0 ]]; do
        if [[ "$is_alternative" -eq "1" ]]; then
            $command
        else
            wget -O /dev/stdout http://localhost/test.html | grep "hello world" 1>&2
        fi

        if [[ $? -eq 0 ]]; then
            echo "Docker container with httpd server successfully started"
            return 0
        else
            echo "Waiting for httpd webserver to start..."
            sleep 2
        fi
        attempts=$(( attempts - 1 ))
    done

    return 1
}

test_docker_containers() {
    echo "Checking that docker is able to create containers."
    echo "Downloading and starting busybox container"
    docker run --name=testhttpd -p 127.0.0.1:80:80/tcp \
        --rm busybox sh -c "echo 'hello world' > /var/www/test.html && httpd -f -p 80 -h /var/www" &

    wait_url_available
    rc=$?
    docker kill -s SIGKILL testhttpd 1>&2
    if [[ ! ${rc} -eq "0" ]]; then
        echo "Timeout error, httpd server is not available, aborting test"
        return 1
    fi
    echo "Success, container is running and serving website properly"
    return 0
}

_check_connection_in_container() {
    ip=$(docker inspect -f '{{range.NetworkSettings.Networks}}{{.IPAddress}}{{end}}' testnetwork)
    docker run --net test-net --name testclient \
        --rm busybox sh -c "wget -O /dev/stdout http://${ip}/test.html | grep 'hello world'" 1>&2
}

test_docker_network () {
    echo "Checking that docker network is working"
    echo "Starting multiple containers in same network"
    docker network create test-net 1>&2

    docker run --net test-net --name testnetwork \
        --rm busybox sh -c "echo 'hello world' > /var/www/test.html && httpd -f -p 80 -h /var/www" 1>&2 &

    wait_url_available 1 '_check_connection_in_container'
    rc=$?
    docker kill -s SIGKILL testnetwork 1>&2
    docker network rm test-net 1>&2

    if [[ ! ${rc} -eq "0" ]]; then
        echo "Timeout error, httpd server is not available, aborting test"
        return 1
    fi

    echo "Success, docker network is operating normally"
    return 0
}

exit_code=0

prepare || exit_code=$(( exit_code + 1 ))

if [[ ${exit_code} -eq 0 ]]; then
    echo "++++++++++++++++++++++++++++++++"
    test_docker_containers || exit_code=$(( exit_code + 1 ))
    echo "++++++++++++++++++++++++++++++++"
    test_docker_network || exit_code=$(( exit_code + 1 ))
    echo "++++++++++++++++++++++++++++++++"
fi

teardown || exit_code=$(( exit_code + 1 ))

exit ${exit_code}