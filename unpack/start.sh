#!/bin/bash

[[ -d /extracted/files ]] || mkdir /extracted/files

function unpack {
    cd /files || exit
    first=$(find . -type f | sed -E 's#^\./##' | grep -vE '^(\.gitignore|unpack|done|running|added_urls)$' | sort | head -1)
    echo "First file: ${first}"
    if [[ "${ZIP_PASSWORD}" != "" ]]; then
        echo "Unpack with password:"
        /7zz x -y -p"${ZIP_PASSWORD}" -o/extracted/files "${first}"
    else
        echo "Unpack without password."
        /7zz x -y -o/extracted/files "${first}"
    fi
}


function pst_extract {
    filename="${1}"
    sha=$(sha256sum "${filename}" | awk '{print $1}')
    if [[ -e "/extracted/files/${sha}" ]]; then
        return
    else
        mkdir -p "/extracted/files/${sha}"
    fi
    if readpst -D -S -j 2 -q -r -o "/extracted/files/${sha}" "${filename}"; then
        if grep "pst_archive=true" /deis.cfg > /dev/null ; then
            mv "${filename}" /extracted/archive
        elif grep "pst_remove=true" /deis.cfg > /dev/null ; then
            rm -f "${filename}"
        fi
    fi
}


function zip_extract {
    filename="${1}"
    sha=$(sha256sum "${filename}" | awk '{print $1}')
    if [[ -e "/extracted/files/${sha}" ]]; then
        return
    else
        mkdir -p "/extracted/files/${sha}"
    fi
    if /7zz x -y -o"/extracted/files/${sha}" "${filename}"; then
        if grep "zip_archive=true" /deis.cfg > /dev/null ; then
            mv "${filename}" /extracted/archive
        elif grep "zip_remove=true" /deis.cfg > /dev/null ; then
            rm -f "${filename}"
        fi
    fi
}


function handle_pst {
    if grep "pst_archive=true" /deis.cfg > /dev/null ; then
        [[ -d /extracted/archive ]] || mkdir /extracted/archive
    fi
    while IFS= read -r -d '' filename; do
        pst_extract "$filename"
    done < <(find /extracted/files -type f -iname '*.pst' -print0)
}


function handle_zip {
    if grep "zip_archive=true" /deis.cfg > /dev/null ; then
        [[ -d /extracted/archive ]] || mkdir /extracted/archive
    fi
    while IFS= read -r -d '' filename; do
        zip_extract "$filename"
    done < <(find /extracted/files -regextype 'egrep' -iregex ".*\.(zip|gz|7z|gzip)" -print0)
}


function summary {
    find /extracted/files -type f -exec basename {} \; | grep -E '^[^.]+\.' | sed 's/^.*\.//' | sort | uniq -c | sort -nr > /extracted/extensions.txt
    find /extracted/files -type f -exec file -b --mime-type {} \; | sort | uniq -c | sort -nr > /extracted/mime.txt
    find /extracted/files > /extracted/files/path.txt
}


function prepare {
    if grep "unpack=true" /deis.cfg > /dev/null ; then
        echo "Start unpack"
        unpack
    else
        return
    fi
    if grep "unzip=true" /deis.cfg > /dev/null ; then
        echo "Start handle_zip"
        handle_zip
    fi
    if grep "pst=true" /deis.cfg > /dev/null ; then
        echo "Start handle_pst"
        handle_pst
    fi
    if grep "summary=true" /deis.cfg > /dev/null ; then
        echo "Start summary"
        summary
    fi
    echo "Unpack done."
}


while true; do
    if [[ -f /files/unpack && ! -e /extracted/files/done ]]; then
        echo "Configuration:"
        cat /deis.cfg
        echo ""
        prepare
        touch /extracted/files/done
        exit
    fi
    sleep 5
done
