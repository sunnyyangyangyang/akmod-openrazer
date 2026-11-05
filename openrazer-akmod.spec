# OpenRazer Akmod RPM Spec - NVIDIA-style approach
#
# This spec is the single source of truth: it builds the userspace packages,
# generates the kmod SRPM and installs it under /usr/src/akmods so that akmods
# can compile the kernel modules on the target machine, for every kernel.

%global _debugsource_packages 0
%global _debuginfo_packages 0
%global debug_package %{nil}
%global _dracut_conf_d /usr/lib/dracut/dracut.conf.d
%global openrazer_version 3.12.4

Name:           openrazer
Version:        %{openrazer_version}
Release:        1%{?dist}
Summary:        Razer device drivers with akmod kernel modules

License:        GPL-2.0-or-later
URL:            https://github.com/openrazer/openrazer
Source0:        %{url}/archive/refs/tags/v%{version}.tar.gz#/%{name}-%{version}.tar.gz
Source1:        openrazer-kmod.spec.in

# Akmod BuildRequires
BuildRequires:  kmodtool
BuildRequires:  akmods
BuildRequires:  gcc make rpm-build
BuildRequires:  systemd-rpm-macros
BuildRequires:  kernel-devel
BuildRequires:  python3-devel
BuildRequires:  python3-setuptools

# Runtime Requirements
Requires:       systemd
%ifarch aarch64 x86_64
Requires:       mokutil
%endif
Requires:       %{name}-kmod = %{?epoch:%{epoch}:}%{version}-%{release}
Requires:       %{name}-kmod-common = %{?epoch:%{epoch}:}%{version}-%{release}
Recommends:     %{name}-daemon = %{?epoch:%{epoch}:}%{version}-%{release}

# Generate akmod metadata. This creates the akmod-openrazer and kmod-openrazer
# subpackages and defines %kmod_pkg_name / %pkg_kmod_name for the templates.
%{expand:%(kmodtool --target %{_target_cpu} --kmodname %{name} --akmod )}

%description
OpenRazer is a collection of Linux drivers for Razer devices: keyboards,
mice, Kraken headsets and accessories such as mouse mats and speakers.

This package provides the udev rules, the razer_mount helper, the AppStream
metadata and the akmod source for the razerkbd, razermouse, razerkraken and
razeraccessory kernel modules. The modules are built automatically by akmods
during installation and rebuilt for every new kernel. No system reboot is
required.

%package kmod-common
Summary:        Common files for %{name} kernel modules
Provides:       %{name}-kmod-common = %{?epoch:%{epoch}:}%{version}-%{release}

%description kmod-common
This package provides the common files for the %{name} kernel modules.

%package daemon
Summary:        OpenRazer daemon
BuildArch:      noarch
Requires:       %{name} = %{?epoch:%{epoch}:}%{version}-%{release}
Requires:       python3-dbus
Requires:       python3-gobject
Requires:       python3-pyudev
Requires:       python3-setproctitle
Requires:       python3-daemonize

%description daemon
The OpenRazer daemon talks to the kernel drivers over sysfs, exposes org.razer
on the session bus and provides persistence and per-device effects support for
Razer hardware. It runs as a user service.

%package -n python3-openrazer
Summary:        Python 3 library for OpenRazer
BuildArch:      noarch
Requires:       %{name}-daemon = %{?epoch:%{epoch}:}%{version}-%{release}
Requires:       python3-dbus
Requires:       python3-numpy

%description -n python3-openrazer
Python library for accessing the OpenRazer daemon from Python.

%prep
%autosetup -n %{name}-%{version}

# Pack the pristine upstream tree for the kmod SRPM. akmods rebuilds this SRPM
# on the target machine, where the kernel modules are compiled with upstream's
# own Makefile (the 'driver' target of the top-level Makefile).
tar -czf %{_builddir}/openrazer-kmod-%{version}.tar.gz \
    --transform "s|^%{name}-%{version}|openrazer-kmod-%{version}|" \
    -C %{_builddir} %{name}-%{version}

%build
# Build the Python bits exactly the way upstream does (setuptools in-tree).
pushd daemon
%{__python3} setup.py build
popd
pushd pylib
%{__python3} setup.py build
popd

%install
# Userspace: drive the upstream packaging targets directly, with DESTDIR.
make -C daemon install DESTDIR=%{buildroot} PREFIX=%{_prefix}
make -C pylib install DESTDIR=%{buildroot} PREFIX=%{_prefix}
make udev_install DESTDIR=%{buildroot} UDEV_PREFIX=%{_prefix}
make appstream_install DESTDIR=%{buildroot} PREFIX=%{_prefix}

# --- Dracut configuration ---
install -d -m 0755 %{buildroot}%{_dracut_conf_d}
cat > %{buildroot}%{_dracut_conf_d}/99-openrazer.conf << 'EOF'
# Do not include the OpenRazer HID drivers in the initramfs.
omit_drivers+=" razerkbd razermouse razerkraken razeraccessory "
EOF

# --- Create and install the kmod SRPM for akmods ---
install -d %{buildroot}%{_usrsrc}/akmods/

SRPM_TOPDIR=$(mktemp -d)
mkdir -p "$SRPM_TOPDIR"/{SOURCES,SPECS}

sed -e 's|@OPENRAZER_VERSION@|%{version}|g' \
    -e 's|@RELEASE@|%{release}|g' \
    %{SOURCE1} > "$SRPM_TOPDIR"/SPECS/openrazer-kmod.spec

cp %{_builddir}/openrazer-kmod-%{version}.tar.gz "$SRPM_TOPDIR"/SOURCES/

rpmbuild -bs \
  --define "_topdir $SRPM_TOPDIR" \
  "$SRPM_TOPDIR"/SPECS/openrazer-kmod.spec

install -m 0644 "$SRPM_TOPDIR"/SRPMS/*.src.rpm %{buildroot}%{_usrsrc}/akmods/

SRPM_NAME=$(basename "$SRPM_TOPDIR"/SRPMS/*.src.rpm)
ln -s "$SRPM_NAME" %{buildroot}%{_usrsrc}/akmods/%{name}-kmod.latest

rm -rf "$SRPM_TOPDIR"

%post
# Generate akmods signing key if it doesn't exist
# This uses the official kmodgenca tool from akmods package
if [ ! -f /etc/pki/akmods/certs/public_key.der ]; then
    echo "Generating akmods signing keys..."
    /usr/sbin/kmodgenca -a 2>/dev/null || true
fi

# === SMART MOK DETECTION ===
smart_mok_check() {
    local akmods_key="/etc/pki/akmods/certs/public_key.der"

    # Early exit: Not a UEFI system
    if [ ! -d /sys/firmware/efi/efivars ]; then
        return 0
    fi

    # Early exit: mokutil not available
    if ! command -v mokutil >/dev/null 2>&1; then
        return 0
    fi

    # Check if Secure Boot is enabled
    local sb_state
    sb_state=$(mokutil --sb-state 2>/dev/null)

    if ! echo "$sb_state" | grep -q "SecureBoot enabled"; then
        return 0
    fi

    # At this point: UEFI + Secure Boot enabled

    # Key should exist now (we just generated it)
    if [ ! -f "$akmods_key" ]; then
        cat << 'EOF'

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
⚠️  SECURE BOOT WARNING
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Failed to generate akmods key. After reboot, run:
  sudo mokutil --import /etc/pki/akmods/certs/public_key.der
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

EOF
        return 1
    fi

    # Check if key is already enrolled
    if mokutil --list-enrolled 2>/dev/null | grep -q "CN=akmods" || \
       mokutil --test-key "$akmods_key" 2>&1 | grep -qi "already.*enrolled"; then
        return 0
    fi

    # Key exists but NOT enrolled - show instructions
    cat << 'EOF'

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🔐 SECURE BOOT DETECTED - ACTION REQUIRED
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
To use OpenRazer with Secure Boot, enroll the MOK key NOW (before reboot):

  sudo mokutil --import /etc/pki/akmods/certs/public_key.der

You'll be asked to create a password. Remember it for the next reboot.

After rebooting:
  1. MOK Manager will appear
  2. Select "Enroll MOK"
  3. Enter the password you just created
  4. Reboot again

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

EOF
}

smart_mok_check || true

# Pick up the freshly installed udev rules
udevadm control --reload-rules >/dev/null 2>&1 || true

cat << 'EOF'

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
✅ OpenRazer Installation Complete
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

The Razer kernel modules (razerkbd, razermouse, razerkraken, razeraccessory)
are being compiled by akmods automatically.
NO REBOOT REQUIRED.

If you installed the daemon, enable it for your user session:
  systemctl --user enable --now openrazer-daemon

Daemon status / log:
  systemctl --user status openrazer-daemon
  journalctl --user -u openrazer-daemon -b

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

EOF

%preun
if [ $1 -eq 0 ]; then
    # Complete uninstall: try to remove the kernel modules
    # (May fail if in use - that's OK, reboot will clear it)
    /sbin/modprobe -r razerkbd razermouse razerkraken razeraccessory >/dev/null 2>&1 || true
fi

%postun
udevadm control --reload-rules >/dev/null 2>&1 || true

if [ $1 -ne 0 ]; then
    cat << 'EOF'

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
✅ OpenRazer Upgraded Successfully
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

akmods is recompiling the Razer HID modules for your current kernel.
NO REBOOT REQUIRED.

Restart the daemon to reload the updated module and tools:
  systemctl --user restart openrazer-daemon

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

EOF
fi

%post daemon
%systemd_user_post openrazer-daemon.service

%preun daemon
%systemd_user_preun openrazer-daemon.service

%postun daemon
%systemd_user_postun_with_restart openrazer-daemon.service

%files
%license LICENSES/GPL-2.0-or-later.txt LICENSES/CC-BY-SA-4.0.txt
%doc README.md
%{_udevrulesdir}/99-razer.rules
%{_udevrulesdir}/../razer_mount
%{_datadir}/metainfo/io.github.openrazer.openrazer.metainfo.xml
%{_dracut_conf_d}/99-openrazer.conf

%files kmod-common
# Empty dependency anchor package

%files daemon
%{python3_sitelib}/openrazer_daemon/
%{python3_sitelib}/openrazer_daemon-*.egg-info/
%{_bindir}/openrazer-daemon
%{_datadir}/openrazer/razer.conf.example
%{_datadir}/dbus-1/services/org.razer.service
%{_userunitdir}/openrazer-daemon.service
%{_mandir}/man5/razer.conf.5.gz
%{_mandir}/man8/openrazer-daemon.8.gz

%files -n python3-openrazer
%{python3_sitelib}/openrazer/
%{python3_sitelib}/openrazer-*.egg-info/

%changelog
* Thu Nov 05 2026 Sunny Yang <yxh9956@gmail.com> - 3.12.4-1
- Initial akmod packaging for Fedora, based on the coreFreq-rpm-fedora
  architecture (one spec is the single source of truth)
- Build the udev rules, razer_mount, AppStream metadata, the user daemon,
  the python library and the akmod carrier from a single spec
- Generate the openrazer-kmod SRPM during %install and ship it with the
  openrazer-kmod.latest symlink so akmods builds it for every kernel
- Compile the modules from the unmodified upstream tree with upstream's own
  Makefile ('driver' target, driver/Makefile) and install the userspace with
  upstream's make targets (daemon, pylib, udev, appstream)
- Add the dracut drop-in, Secure Boot/MOK handling, kmod-common anchor,
  kmod meta package and the post-install/post-upgrade banners
- No packaging build helper is shipped; plain rpmbuild is the build entry point
