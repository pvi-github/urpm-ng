#!/usr/bin/bash

. /etc/lsb-release && cd /home/superadmin/Sources && rm -fr urpm-ng2 && cp -a urpm-ng urpm-ng2 && cd urpm-ng2 && rm -f rpmbuild/SRPMS/*.src.rpm rpmbuild/RPMS/noarch/*.rpm && make version && make rpm
