project('pk-backend-urpm', 'c',
  version : '0.1.0',
  license : 'GPL-3.0-or-later',
  default_options : [
    'c_std=c11',
    'warning_level=2',
  ],
)

# Dependencies
glib = dependency('glib-2.0', version : '>= 2.56')
gio = dependency('gio-2.0')
json_glib = dependency('json-glib-1.0')
packagekit_glib2 = dependency('packagekit-glib2', version : '>= 1.0')

# Get PackageKit backend directory
pk_backend_dir = get_option('pk_backend_dir')
if pk_backend_dir == ''
  pk_backend_dir = join_paths(get_option('libdir'), 'packagekit-backend')
endif

# Build the backend shared library
# Note: pk-backend.h and pk-backend-job.h are bundled from PackageKit source
# because Mageia doesn't ship the backend development headers
# The pk_backend_job_* symbols are provided by PackageKit at runtime (module loading)
shared_library('pk_backend_urpm',
  'pk-backend-urpm.c',
  dependencies : [glib, gio, json_glib, packagekit_glib2],
  link_args : ['-Wl,--unresolved-symbols=ignore-all'],
  install : true,
  install_dir : pk_backend_dir,
)
