# Base image plus one repair, applied before any devcontainer feature installs.
#
# THE PROBLEM
# The upstream devcontainers/python image ships an APT source for Yarn
# (dl.yarnpkg.com). Yarn rotated its signing key, so that repository now fails
# verification:
#
#   NO_PUBKEY 62D54FD4003F6525
#
# `apt-get update` treats that as fatal. The docker-in-docker feature runs
# apt-get update as its first step, so the feature install fails and Codespace
# creation fails with it.
#
# WHY THIS FILE EXISTS AT ALL
# Devcontainer lifecycle commands (onCreate/postCreate) all run *after*
# features are installed, so none of them can repair the sources list in time.
# A Dockerfile is the only stage that runs earlier, which is why the fix lives
# here rather than in a script.
#
# WHY REMOVE RATHER THAN RE-KEY
# Importing the new Yarn key would also work, but it would leave this build
# depending on a third-party signing key that has already rotated once, for a
# package manager nothing in this repository uses. This is a Python benchmark;
# it needs no Yarn and no Node. Removing the repository is both simpler and
# one less thing that can break the environment later.

FROM mcr.microsoft.com/devcontainers/python:1-3.12-bookworm

USER root

RUN set -eux; \
    # Drop any third-party source that points at Yarn. Both the legacy .list
    # form and the newer deb822 .sources form are handled, and the match is by
    # content rather than by filename so a renamed file cannot slip through.
    if [ -d /etc/apt/sources.list.d ]; then \
        grep -rlE 'yarnpkg' /etc/apt/sources.list.d/ 2>/dev/null | xargs -r rm -f; \
    fi; \
    # The main sources.list is edited rather than deleted: it carries Debian
    # itself, and removing it would be a far worse failure than the one being
    # fixed.
    if [ -f /etc/apt/sources.list ]; then \
        sed -i -E '/yarnpkg/d' /etc/apt/sources.list; \
    fi; \
    # Prove the repair worked now, at build time, instead of discovering it
    # when a feature runs apt-get update and the Codespace dies.
    apt-get update; \
    rm -rf /var/lib/apt/lists/*

USER vscode
