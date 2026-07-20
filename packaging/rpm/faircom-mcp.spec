Name:           faircom-mcp
Version:        0.1.0
Release:        1%{?dist}
Summary:        FairCom MCP server

License:        Proprietary
URL:            https://faircom.com/
BuildArch:      noarch

Requires:       python3
Requires:       python3-pip
Requires:       ca-certificates
Requires:       systemd

%description
Production-grade MCP server for the FairCom JSON API.

%prep
%build

%install
install -D -m 0644 packaging/systemd/faircom-mcp.service %{buildroot}%{_unitdir}/faircom-mcp.service
install -D -m 0644 packaging/systemd/faircom-mcp.env.example %{buildroot}%{_sysconfdir}/faircom-mcp/faircom-mcp.env
install -D -m 0644 packaging/logrotate/faircom-mcp %{buildroot}%{_sysconfdir}/logrotate.d/faircom-mcp
install -D -m 0644 packaging/sysusers.d/faircom-mcp.conf %{buildroot}%{_sysusersdir}/faircom-mcp.conf
install -D -m 0644 packaging/tmpfiles.d/faircom-mcp.conf %{buildroot}%{_tmpfilesdir}/faircom-mcp.conf

%files
%{_unitdir}/faircom-mcp.service
%config(noreplace) %{_sysconfdir}/faircom-mcp/faircom-mcp.env
%config(noreplace) %{_sysconfdir}/logrotate.d/faircom-mcp
%{_sysusersdir}/faircom-mcp.conf
%{_tmpfilesdir}/faircom-mcp.conf

%post
systemd-sysusers faircom-mcp.conf || :
systemd-tmpfiles --create faircom-mcp.conf || :
systemctl daemon-reload || :

%preun
if [ "$1" -eq 0 ]; then
  systemctl stop faircom-mcp.service || :
fi

%postun
systemctl daemon-reload || :