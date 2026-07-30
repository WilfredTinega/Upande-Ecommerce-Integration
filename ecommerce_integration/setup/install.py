import os

import frappe
from frappe.modules.utils import reload_doc


def after_install():
	resync_app_resources()
	normalize_ecommerce_workspace()
	ensure_desktop_icon()


def after_migrate():
	resync_app_resources()
	normalize_ecommerce_workspace()
	ensure_desktop_icon()


# Frappe's migrate skips JSON resources when the DB record's `modified` is newer
# than the file (see frappe/modules/import_file.py). UI edits or other apps'
# after_migrate hooks bump that timestamp, so workspace/page/etc. updates we ship
# silently never reach the site. This helper force-reloads every JSON resource
# the app owns, bypassing the timestamp + hash check. Safe to run repeatedly.
_RESOURCE_DIRS = (
	"doctype",
	"page",
	"report",
	"print_format",
	"notification",
	"workspace",
	"web_template",
	"web_form",
	"web_page",
	"dashboard",
	"dashboard_chart",
	"number_card",
	"module_onboarding",
	"onboarding_step",
	"form_tour",
	"client_script",
	"server_script",
	"custom",
)


def resync_app_resources():
	"""Force-reload every JSON resource this app ships, ignoring DB-vs-file
	timestamps. Safe to run repeatedly."""
	module_root = frappe.get_app_path("ecommerce_integration", "ecommerce_integration")
	module_name = "Ecommerce Integration"

	for dt in _RESOURCE_DIRS:
		dt_root = os.path.join(module_root, dt)
		if not os.path.isdir(dt_root):
			continue
		for dn in os.listdir(dt_root):
			doc_dir = os.path.join(dt_root, dn)
			if not os.path.isdir(doc_dir):
				continue
			if not os.path.exists(os.path.join(doc_dir, f"{dn}.json")):
				continue
			try:
				reload_doc(module_name, dt, dn, force=True)
			except Exception:
				frappe.log_error(
					title=f"ecommerce_integration resync_app_resources: {dt}/{dn}",
					message=frappe.get_traceback(),
				)


# Frappe requires a Workspace's name == title == label, and derives the Desk
# route from slug(name) (frappe/public/js/frappe/views/workspace/workspace.js).
# We want the admin workspace to live at /app/ecommerce (slug of "Ecommerce"),
# distinct from the /webshop storefront URL shortcut inside it. A stale install /
# UI edit can leave title or label out of sync, or set parent_page to the
# workspace itself (nesting it under a missing parent, which 404s the icon).
# Normalise all of that here so every install/migrate lands the same working
# state. Safe to run repeatedly.
_WORKSPACE_NAME = "Ecommerce"
# The workspace was first shipped as "Ecommerce Integration" before being renamed
# to "Ecommerce". A site migrated in between keeps that orphaned record (and the
# Desktop Icon auto-generated from it), showing a duplicate workspace/tile. Drop it.
_LEGACY_WORKSPACE_NAME = "Ecommerce Integration"


def normalize_ecommerce_workspace():
	"""Force the Ecommerce Workspace's name/title/label consistent and clear any
	self-referential parent_page so the Desk icon opens /app/ecommerce."""
	# Remove the pre-rename "Ecommerce Integration" workspace if it lingers, so it
	# doesn't show as a second workspace alongside "Ecommerce".
	if frappe.db.exists("Workspace", _LEGACY_WORKSPACE_NAME):
		try:
			frappe.delete_doc(
				"Workspace",
				_LEGACY_WORKSPACE_NAME,
				ignore_permissions=True,
				force=True,
			)
		except Exception:
			frappe.log_error(
				title="ecommerce_integration drop legacy workspace",
				message=frappe.get_traceback(),
			)

	if not frappe.db.exists("Workspace", _WORKSPACE_NAME):
		return

	current = frappe.db.get_value(
		"Workspace",
		_WORKSPACE_NAME,
		["title", "label", "parent_page"],
		as_dict=True,
	)
	needs_fix = (
		current.title != _WORKSPACE_NAME
		or current.label != _WORKSPACE_NAME
		or current.parent_page == _WORKSPACE_NAME
	)
	if not needs_fix:
		return

	try:
		# Write the identity fields directly. Going through doc.save() risks
		# Workspace's on_update rename trigger (it collapses name->title when
		# label == name), which would fight us; a db_set keeps name stable.
		frappe.db.set_value(
			"Workspace",
			_WORKSPACE_NAME,
			{"title": _WORKSPACE_NAME, "label": _WORKSPACE_NAME, "parent_page": ""},
			update_modified=False,
		)
		# The sidebar header (Workspace Sidebar) mirrors the title; keep it in step.
		if frappe.db.exists("Workspace Sidebar", _WORKSPACE_NAME):
			frappe.db.set_value(
				"Workspace Sidebar",
				_WORKSPACE_NAME,
				"title",
				_WORKSPACE_NAME,
				update_modified=False,
			)
	except Exception:
		frappe.log_error(
			title="ecommerce_integration normalize_ecommerce_workspace",
			message=frappe.get_traceback(),
		)


# The launcher tile on /desk and /apps is a Desktop Icon. Frappe auto-generates a
# Desktop Icon from the public Workspace (labelled with the workspace name
# "Ecommerce", linking to the bare "/ecommerce" route that 404s). Upsert our own
# External-link icon every install/migrate and drop the stale auto-generated ones,
# so the tile opens /app/ecommerce.
#
# IMPORTANT: the icon's label MUST equal the Workspace title ("Ecommerce"). The
# Desk sidebar header and the workspace breadcrumb both resolve their icon via
# frappe.utils.get_desktop_icon_by_label(sidebar_title), where sidebar_title is
# the active workspace title. A mismatch means the breadcrumb is silently dropped.
_DESKTOP_ICON_NAME = "Ecommerce"
# Drop the legacy webshop-owned ("Webshop"/"Upande Webshop") icons so only the
# title-matched "Ecommerce" icon remains.
# The Desktop Icon Frappe auto-generates for this app/workspace is labelled with
# the app title ("Ecommerce Integration") and links to the bare "/ecommerce"
# route; drop it so only our title-matched "Ecommerce" icon (-> /app/ecommerce)
# remains. (Kept as a tuple in case more legacy names accrue.)
_STALE_DESKTOP_ICON_NAMES = ("Ecommerce Integration",)


def ensure_desktop_icon():
	"""Create / refresh the launcher Desktop Icon for the Ecommerce workspace."""
	for stale in _STALE_DESKTOP_ICON_NAMES:
		if frappe.db.exists("Desktop Icon", stale):
			frappe.delete_doc(
				"Desktop Icon",
				stale,
				ignore_permissions=True,
				force=True,
			)

	payload = {
		"doctype": "Desktop Icon",
		"name": _DESKTOP_ICON_NAME,
		"label": _DESKTOP_ICON_NAME,
		"app": "ecommerce_integration",
		"icon_type": "App",
		"link_type": "External",
		"link": "/app/ecommerce",
		# `link_to` is a Dynamic Link whose target doctype is read from `link_type`.
		# Frappe's own create_desktop_icons_from_workspace() seeds a "Workspace Sidebar"
		# icon for our public workspace with link_to="Ecommerce"; flipping link_type to
		# "External" without clearing link_to makes link validation resolve a doctype
		# literally named "External" and blow up ("DocType External not found").
		"link_to": None,
		"sidebar": None,
		# Same logo as upande_ta, vendored into this app's own assets so the icon
		# carries no dependency on another app being installed.
		"logo_url": "/assets/ecommerce_integration/images/upande_logo.ico",
		"force_show": 1,
		"hidden": 0,
		"standard": 1,
	}

	if frappe.db.exists("Desktop Icon", _DESKTOP_ICON_NAME):
		doc = frappe.get_doc("Desktop Icon", _DESKTOP_ICON_NAME)
		for k, v in payload.items():
			if k in ("doctype", "name"):
				continue
			doc.set(k, v)
		doc.save(ignore_permissions=True)
	else:
		frappe.get_doc(payload).insert(ignore_permissions=True, ignore_if_duplicate=True)

	frappe.clear_cache()
