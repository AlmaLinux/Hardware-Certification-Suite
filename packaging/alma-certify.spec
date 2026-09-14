%global upstream_name %{name}

# Pinned: a benchmark whose result depends on what a mirror served that day is not
# comparable between machines, and nothing here downloads at run time.
%global clpeak_version 2.1.4

# pin different python versions per release
# el8 - 3.12
# el9 - 3.9 (default python3)
# el10 - python3.12 (default python3)
%if 0%{?rhel} == 8
%global alma_certify_python_pkg python3.12
%else
%global alma_certify_python_pkg python3
%endif
%global alma_certify_python %{_bindir}/%{alma_certify_python_pkg}

%global alma_certify_home %{_datadir}/%{name}

# package is byte-compiled by hand in %%install with the interpreter above
# don't auto compile against different/stock python3 version
%global _python_bytecompile_extra 0
%global __brp_python_bytecompile %{nil}

Name:           alma-certify
Version:        0.1.0
Release:        104%{?dist}
Summary:        AlmaLinux hardware certification suite

License:        MIT
URL:            https://github.com/AlmaLinux/%{upstream_name}
# GitHub's own archive for the v%%{version} tag. .tar.gz because that route serves .tar.gz and
# .zip only, and the tag's leading v is stripped from the directory inside, which is what
# %%autosetup below expects.
Source0:        %{url}/archive/v%{version}/%{upstream_name}-%{version}.tar.gz
Source1:        https://github.com/krrishnarraj/clpeak/archive/%{clpeak_version}/clpeak-%{clpeak_version}.tar.gz

BuildArch:      noarch
# dmidecode is not built for ppc64le or s390x
ExclusiveArch:  x86_64 aarch64

# Install alias
Provides:       almalinux-certify = %{version}-%{release}

BuildRequires:  %{alma_certify_python_pkg}-devel
# el8's python3.12-devel doesn't pull this in and we need it
%if 0%{?rhel} == 8
BuildRequires:  python3-rpm-macros
%endif
BuildRequires:  %{alma_certify_python_pkg}-pytest

Requires:       %{alma_certify_python_pkg}
%if 0%{?rhel} != 8
Requires:       python3 >= 3.9
%endif
# Result bundles are .tar.zst.
Requires:       zstd

# clpeak is shipped in this package and built on the host being tested
Provides:       bundled(clpeak) = %{clpeak_version}
# alma_certify/_qrcodegen.py, verbatim from Project Nayuki (MIT); its notice is in the file
Provides:       bundled(qrcodegen) = 1.8.0

# Collection stage
Requires:       dmidecode
Requires:       pciutils
Requires:       usbutils
Requires:       util-linux
Requires:       ethtool
Requires:       smartmontools
Requires:       nvme-cli

%description
alma-certify runs three kinds of certification runs on AlmaLinux systems:
hardware inventory collection, functional validation tests, and benchmarks.
Results are written as a versioned JSON report plus raw artifacts, bundled
into a tarball that can be submitted to the AlmaLinux certification web
application (Lumina) online or uploaded manually offline.

%package tui
Summary:        Rich full-screen (Textual) interface for %{name}
Requires:       %{name} = %{version}-%{release}
Requires:       %{alma_certify_python_pkg}

# Install alias
Provides:       almalinux-certify-tui = %{version}-%{release}

# Vendored under alma_certify_tui/_vendor because Textual is not packaged across all EPEL
# releases this suite targets. Regenerate the tree and this block with
# packaging/vendor/update_vendor.py, which prints these Provides and the License.
License:        MIT AND PSF-2.0
# MIT
Provides:       bundled(python3dist(textual)) = 8.2.8
# MIT
Provides:       bundled(python3dist(rich)) = 15.0.0
# MIT
Provides:       bundled(python3dist(markdown-it-py)) = 3.0.0
# MIT
Provides:       bundled(python3dist(mdurl)) = 0.1.2
# MIT
Provides:       bundled(python3dist(mdit-py-plugins)) = 0.4.2
# MIT
Provides:       bundled(python3dist(linkify-it-py)) = 2.0.3
# MIT
Provides:       bundled(python3dist(uc-micro-py)) = 1.0.3
# MIT
Provides:       bundled(python3dist(platformdirs)) = 4.4.0
# PSF-2.0
Provides:       bundled(python3dist(typing-extensions)) = 4.16.0

# injected local shims
#   pygments  - Our own MIT null-highlighter shim, not the real Pygments. See patches/pygments/__init__.py.

%description tui
An optional full-screen, mouse-driven interface for %{name}, built on Textual. It collects the run
choices, prints the command they add up to, and hands off to the same commands as the CLI, with a
richer display that also works well over SSH.

The base package has no full-screen interface of its own; installing this adds `alma-certify tui`.
Textual and its dependencies are bundled because they are not available as packages on every
supported EPEL release.

%prep
%autosetup -n %{upstream_name}-%{version}

%build

%install
install -d %{buildroot}%{alma_certify_home}
cp -a alma_certify %{buildroot}%{alma_certify_home}/

# Traces a stored result back to the suite build that measured it.
echo "%{version}-%{release}" > %{buildroot}%{alma_certify_home}/alma_certify/data/BUILDINFO

install -p -m 0644 %{SOURCE1} \
    %{buildroot}%{alma_certify_home}/alma_certify/data/clpeak-%{clpeak_version}.tar.gz

# Installed privately here rather than into %%{python3_sitelib} by the pyproject macros
%py_byte_compile %{alma_certify_python} %{buildroot}%{alma_certify_home}/alma_certify

# A sibling, not a submodule, so the base package stays stdlib-only
cp -a alma_certify_tui %{buildroot}%{alma_certify_home}/
%py_byte_compile %{alma_certify_python} %{buildroot}%{alma_certify_home}/alma_certify_tui || :

install -d %{buildroot}%{_bindir}
# A launcher rather than a console script, because this package's interpreter is not the
# default python3 on every supported release. -I (not -P, which does less and does not
# exist before 3.11) keeps the working directory, PYTHONPATH, and user site off sys.path
# for a tool that runs as root.
cat > %{buildroot}%{_bindir}/%{name} <<EOF
#!/bin/sh
exec %{alma_certify_python} -I -c 'import sys; sys.path.insert(0, "%{alma_certify_home}"); from alma_certify.cli import main; sys.exit(main())' "\$@"
EOF
chmod 0755 %{buildroot}%{_bindir}/%{name}

install -d %{buildroot}%{_mandir}/man1
install -p -m 0644 %{name}.1 %{buildroot}%{_mandir}/man1/%{name}.1

install -d %{buildroot}%{_sysconfdir}/%{name}
install -p -m 0644 %{name}.conf \
    %{buildroot}%{_sysconfdir}/%{name}/%{name}.conf

install -d %{buildroot}%{_sharedstatedir}/%{name}/runs

# The submission token lands here, written 0600 by `alma-certify register`.
install -d %{buildroot}%{_localstatedir}/cache/%{name}

# Every bundled license text in one tree, for %%files to mark. mdit-py-plugins carries a per-plugin
# notice for code it vendored in turn; those are separate copyrights.
mkdir -p bundled
cp -a alma_certify_tui/_vendor/licenses/. bundled/
for notice in alma_certify_tui/_vendor/mdit_py_plugins/*/LICENSE; do
    test -f "$notice" || continue
    install -Dp -m 0644 "$notice" \
        "bundled/mdit-py-plugins/$(basename "$(dirname "$notice")").LICENSE"
done
rm -rf %{buildroot}%{alma_certify_home}/alma_certify_tui/_vendor/licenses
rm -f %{buildroot}%{alma_certify_home}/alma_certify_tui/_vendor/mdit_py_plugins/*/LICENSE

%check
test -s %{buildroot}%{alma_certify_home}/alma_certify/data/BUILDINFO

# Probe sources and data the suite compiles or reads on the machine under test
for datafile in openclprobe.c vulkanprobe.c cudaprobe.cu memlat.c streamish.c \
                corpus.py dmesg-patterns.conf; do
    test -f %{buildroot}%{alma_certify_home}/alma_certify/data/$datafile || {
        echo "error: alma_certify/data/$datafile is missing from the package" >&2
        exit 1
    }
done

PYTHONPATH=%{buildroot}%{alma_certify_home} %{alma_certify_python} -m pytest -q tests

# module imports
%{alma_certify_python} -I -c 'import importlib, pkgutil, sys; \
    sys.path.insert(0, "%{buildroot}%{alma_certify_home}"); \
    import alma_certify; \
    [importlib.import_module(module.name) \
     for module in pkgutil.walk_packages(alma_certify.__path__, "alma_certify.")]'

sh -n %{buildroot}%{_bindir}/%{name}
grep -q -- ' -I -c ' %{buildroot}%{_bindir}/%{name}
%{alma_certify_python} -I -c 'import sys; \
    sys.path.insert(0, "%{buildroot}%{alma_certify_home}"); \
    from alma_certify.cli import main; \
    sys.exit(main(["--help"]))' >/dev/null

# test full stack import with our local pygments shim
%{alma_certify_python} -I -c 'import sys; \
    sys.path.insert(0, "%{buildroot}%{alma_certify_home}"); \
    import alma_certify_tui, alma_certify_tui.app; \
    import pygments; \
    assert getattr(pygments, "__vendored_stub__", False), "not the pygments shim"; \
    import textual.app, rich.traceback, textual.highlight; \
    print("alma-certify-tui import check OK")'

%files
%license LICENSE
%doc README.md docs/
%{_bindir}/%{name}
%{_mandir}/man1/%{name}.1*
%dir %{alma_certify_home}
%{alma_certify_home}/alma_certify/
%dir %{_sysconfdir}/%{name}
%config(noreplace) %{_sysconfdir}/%{name}/%{name}.conf
%dir %attr(0750,root,root) %{_sharedstatedir}/%{name}
%dir %attr(0750,root,root) %{_sharedstatedir}/%{name}/runs
# 0700: this holds the submission token.
%dir %attr(0700,root,root) %{_localstatedir}/cache/%{name}
# Written by `alma-certify register`, so it is owned but not shipped, and goes on erase.
%ghost %attr(0600,root,root) %{_localstatedir}/cache/%{name}/token.json

%files tui
%license LICENSE
%license bundled/
%{alma_certify_home}/alma_certify_tui/

%changelog
* Mon Sep 14 2026 Jonathan Wright <jonathan@almalinux.org> - 0.1.0-104
- Initial package: rewritten Python suite replacing the Ansible prototype
