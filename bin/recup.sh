#!/usr/bin/bash

SOURCE=/home/superadmin/Sources/urpm-ng2
DEST=/home/superadmin/Sources/urpm-ng

cd $SOURCE

find rpmbuild -name "*.rpm" | while read package
do
	echo "cp -f $package $DEST/$package"
	cp -f $package $DEST/$package
done

