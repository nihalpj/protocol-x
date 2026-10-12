# OPA egress policy for the OpenShell outer cage (F-034):
# only Nebius Token Factory inference and package registries may egress (INV-8).
package protox.egress

default allow := false

allowed_hosts := {
	"api.tokenfactory.nebius.com",
	"pypi.org",
	"files.pythonhosted.org",
}

allow if {
	some host in allowed_hosts
	input.destination.host == host
}
