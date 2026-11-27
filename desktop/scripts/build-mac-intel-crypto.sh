#!/bin/bash
set -euo pipefail

payload_python=$1
wheel_dir=$2
build_lock=$3
cd "$wheel_dir"

verified_archive() {
    local address=$1 archive=$2 digest=$3
    curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' --retry 2 \
        --max-time 180 --output "$archive" "$address"
    printf '%s  %s\n' "$digest" "$archive" | shasum -a 256 -c -
    tar -xzf "$archive"
}

# PyPI's newest Intel wheel predates fixes shipped in cryptography 50. Rebuild from the
# pinned PyPI source and OpenSSL 3.5 LTS release published by their maintainers.
verified_archive 'https://github.com/openssl/openssl/releases/download/openssl-3.5.9/openssl-3.5.9.tar.gz' \
    openssl.tar.gz 603f5602e2eef00d77fbd429d34dcd5822bb301757a1bc9cdb24c670f1eb859a
verified_archive 'https://files.pythonhosted.org/packages/9d/af/182eb91b0df3fe75c4d9f26fe70684569566745f6ba7e5c9c73a862c5252/cryptography-50.0.2.tar.gz' \
    cryptography.tar.gz 7b46165bb56eb4704e2eaaf86f3c940d19154535d9b0ca7d6d590b04060e00d5

export MACOSX_DEPLOYMENT_TARGET=12.0
(
    cd openssl-3.5.9
    ./Configure darwin64-x86_64-cc no-shared no-tests --prefix="$wheel_dir/openssl" --libdir=lib
    make -s -j "$(sysctl -n hw.ncpu)"
    make -s install_sw
)

"$payload_python" -I -m venv "$wheel_dir/build-python"
build_python="$wheel_dir/build-python/bin/python"
"$build_python" -I -m pip --isolated install --index-url https://pypi.org/simple \
    --only-binary=:all: --require-hashes -r "$build_lock"

cd cryptography-50.0.2
cargo fetch --locked --target x86_64-apple-darwin
PATH="$wheel_dir/build-python/bin:$PATH" OPENSSL_DIR="$wheel_dir/openssl" OPENSSL_STATIC=1 \
    CARGO_BUILD_TARGET=x86_64-apple-darwin CARGO_NET_OFFLINE=true MATURIN_PEP517_ARGS=--locked \
    "$build_python" -I -m pip --isolated wheel --no-deps --no-build-isolation --wheel-dir "$wheel_dir" .
