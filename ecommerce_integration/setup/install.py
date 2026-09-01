"""Install / migrate setup for Ecommerce Integration.

Everything this app owns but that does NOT live in a plain doctype schema —
JSON resources, nav records, custom fields on other apps' doctypes, per-settings
Scheduled Job Type rows and Log Settings retention — has to be (re)applied by
hand after `bench migrate`, because Frappe either skips it (modified-timestamp
checks) or actively prunes it (scheduler + orphan-entity sweeps).

`after_migrate()` below is the single entry point wired into hooks. It runs every
step, each isolated: a failure is logged and the remaining steps still run, so one
broken piece can never abort the migrate or silently skip the rest of the app.
"""

import os

import frappe

# --------------------------------------------------------------------------- #
# entry points
# --------------------------------------------------------------------------- #

APP_NAME = "ecommerce_integration"
MODULE_NAME = "Ecommerce Integration"


def _steps():
	"""Every after_install / after_migrate action, in dependency order.

	Resources first (doctypes must exist before anything reads or extends them),
	then nav, then the things that hang off the settings doctypes.
	"""
	from ecommerce_integration.ecommerce_integration.doctype.biflorica_setting.biflorica_custom_fields import (
		ensure_biflorica_custom_fields,
	)
	from ecommerce_integration.ecommerce_integration.doctype.biflorica_setting.biflorica_setting import (
		resync_scheduled_jobs as biflorica_resync_scheduled_jobs,
	)
	from ecommerce_integration.ecommerce_integration.doctype.floriday_settings.floriday_custom_fields import (
		ensure_floriday_custom_fields,
	)
	from ecommerce_integration.ecommerce_integration.doctype.floriday_settings.floriday_settings import (
		resync_scheduled_jobs as floriday_resync_scheduled_jobs,
	)
	from ecommerce_integration.ecommerce_integration.doctype.shopify_allocation.shopify_allocation import (
		ensure_packing_link_fields,
	)
	from ecommerce_integration.ecommerce_integration.doctype.shopify_settings.shopify_settings import (
		ensure_log_retention as shopify_ensure_log_retention,
	)
	from ecommerce_integration.ecommerce_integration.doctype.shopify_settings.shopify_settings import (
		resync_scheduled_jobs as shopify_resync_scheduled_jobs,
	)

	return (
		# 1. Force-reload every JSON resource we ship (doctypes, workspace,
		#    desktop icon, sidebar, ...) bypassing Frappe's timestamp/hash skip.
		("resync_app_resources", resync_app_resources),
		# 2. Keep the Ecommerce workspace's name/title/label consistent and
		#    parent_page clear so /app/ecommerce opens.
		("normalize_ecommerce_workspace", normalize_ecommerce_workspace),
		# 3. Collapse any duplicate Desk tiles down to the single icon we ship.
		("enforce_single_desktop_icon", enforce_single_desktop_icon),
		("enforce_single_workspace_sidebar", enforce_single_workspace_sidebar),
		# 4. Restore Scheduled Job Type rows. These are configured per Settings
		#    doc rather than declared in scheduler_events, so Frappe's scheduler
		#    sync deletes them on every migrate.
		("floriday_resync_scheduled_jobs", floriday_resync_scheduled_jobs),
		("biflorica_resync_scheduled_jobs", biflorica_resync_scheduled_jobs),
		("shopify_resync_scheduled_jobs", shopify_resync_scheduled_jobs),
		# 5. Custom fields this app adds to other apps' doctypes.
		("ensure_floriday_custom_fields", ensure_floriday_custom_fields),
		("ensure_biflorica_custom_fields", ensure_biflorica_custom_fields),
		("ensure_packing_link_fields", ensure_packing_link_fields),
		# 6. Shopify API log retention lives in Frappe's Log Settings, which the
		#    Shopify Settings form only writes on save.
		("shopify_ensure_log_retention", shopify_ensure_log_retention),
	)


def _run_all(context):
	"""Run every step, isolating failures so the rest of the app still updates."""
	for label, fn in _steps():
		try:
			fn()
			frappe.db.commit()  # nosemgrep - each step must land independently
		except Exception:
			frappe.db.rollback()
			frappe.log_error(
				title=f"{APP_NAME} {context}: {label}",
				message=frappe.get_traceback(),
			)
			print(f"{APP_NAME} {context}: step '{label}' failed, see Error Log")


def after_install():
	_run_all("after_install")


def after_migrate():
	_run_all("after_migrate")


# --------------------------------------------------------------------------- #
# JSON resources
# --------------------------------------------------------------------------- #

# Frappe's migrate skips a JSON resource when the DB record's `modified` is newer
# than the file, or when a stored hash matches (see frappe/modules/import_file.py).
# UI edits or other apps' after_migrate hooks bump that timestamp, so updates we
# ship silently never reach the site. This helper force-reloads every resource the
# app owns, bypassing those checks.
#
# Note this is deliberately destructive to site-side edits of the records we ship
# (the workspace layout, the sidebar, ...) — the app's files are the source of
# truth, which is the whole point of running it on every migrate.

# Frappe syncs these from the app package root as flat `<name>.json` files rather
# than from a module directory (see frappe.model.sync.sync_for).
_APP_LEVEL_DIRS = ("desktop_icon", "workspace_sidebar", "sidebar_item_group")


def _app_resource_paths():
	"""Every JSON resource file this app ships, in Frappe's own sync order.

	Built from `frappe.model.sync.get_doc_files`, so the set tracks whatever
	Frappe considers importable instead of a hand-maintained list that drifts
	(it also picks up doctype_layout files, which live at a different depth).
	"""
	from frappe.model.sync import get_doc_files
	from frappe.modules.utils import get_module_list

	paths = []
	for module in get_module_list(APP_NAME) or []:
		module_root = frappe.get_app_path(APP_NAME, frappe.scrub(module))
		if os.path.isdir(module_root):
			# Take the return value: get_doc_files() starts with `files = files or []`,
			# so an empty list argument is rebound to a new list and everything it
			# collected for the first module is dropped on the floor.
			paths = get_doc_files(files=paths, start_path=module_root)

	app_root = frappe.get_app_path(APP_NAME)
	for folder in _APP_LEVEL_DIRS:
		folder_path = os.path.join(app_root, folder)
		if not os.path.isdir(folder_path):
			continue
		for filename in sorted(os.listdir(folder_path)):
			if filename.endswith(".json"):
				paths.append(os.path.join(folder_path, filename))

	return paths


def resync_app_resources():
	"""Force-reload every JSON resource this app ships, ignoring DB-vs-file
	timestamps and hashes. Safe to run repeatedly."""
	from frappe.modules.import_file import import_file_by_path
	from frappe.modules.patch_handler import _patch_mode

	# Same guard sync_all() uses: importing a DocType can queue patches, and we
	# are already running inside (or just after) the patch phase.
	_patch_mode(True)
	try:
		for path in _app_resource_paths():
			try:
				import_file_by_path(path, force=True, ignore_version=True)
				frappe.db.commit()  # nosemgrep - keep each resource independent
			except Exception:
				frappe.db.rollback()
				frappe.log_error(
					title=f"{APP_NAME} resync_app_resources: {os.path.basename(path)}",
					message=frappe.get_traceback(),
				)
	finally:
		_patch_mode(False)

	frappe.clear_cache()


# --------------------------------------------------------------------------- #
# workspace identity
# --------------------------------------------------------------------------- #

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


# --------------------------------------------------------------------------- #
# desktop icon
# --------------------------------------------------------------------------- #

# The app ships exactly one Desk tile: desktop_icon/ecommerce.json ("Ecommerce",
# pointing at the "Ecommerce" Workspace Sidebar). Frappe can still end up with
# extras beside it:
#
#   * `create_desktop_icons_from_workspace()` (run by bench install-app /
#     add-to-apps and after some upgrades) makes one icon per public Workspace,
#     de-duplicating only on (label, icon_type) — so the pre-rename "Ecommerce
#     Integration" workspace, or any other public workspace this module owns,
#     produces a second tile for the same destination.
#   * `add_workspace_to_desktop()` / a user saving their Desk layout inserts
#     non-standard icons pointing at the same workspace.
#
# Keep the shipped icon, drop the other tiles that open the same destination.
_CANONICAL_ICON = "Ecommerce"
# Workspace names this app has shipped, current and historical — a tile aimed at
# either of them is aimed at us. `Ecommerce Integration` doubles as this app's
# app_title, so it also catches an auto-generated app tile.
_OWNED_ICON_TARGETS = (_WORKSPACE_NAME, _LEGACY_WORKSPACE_NAME)


def _duplicate_desktop_icons():
	"""Desk tiles that open the same place as the icon we ship.

	Deliberately matched on *destination*, not on `app`: a site can create its
	own extra workspaces in the Desk UI, and Frappe stamps them with this
	module's `app`, so filtering on `app` would delete tiles for workspaces
	someone built here. Frappe's own orphan sweep already removes those when they
	are `standard=1`; anything `standard=0` is the site's to keep.
	"""
	names = set()

	# Tiles pointing at our workspace / sidebar, whatever they are labelled.
	names.update(
		frappe.get_all(
			"Desktop Icon",
			or_filters=[
				["link_to", "in", _OWNED_ICON_TARGETS],
				["sidebar", "in", _OWNED_ICON_TARGETS],
				["label", "in", _OWNED_ICON_TARGETS],
			],
			pluck="name",
		)
	)

	# The App-type tile Frappe derives from `add_to_apps_screen`. This app does
	# not declare that hook today, but a site that once had it keeps the tile.
	names.update(frappe.get_all("Desktop Icon", filters={"icon_type": "App", "app": APP_NAME}, pluck="name"))

	names.discard(_CANONICAL_ICON)
	return sorted(names)


def enforce_single_desktop_icon():
	"""Leave exactly one Desktop Icon for this app: the one we ship.

	Runs after resync_app_resources, so the canonical icon is already (re)created
	from JSON by the time we prune.
	"""
	duplicates = _duplicate_desktop_icons()
	if not duplicates:
		return

	for name in duplicates:
		try:
			# Clear standard/app first. Desktop Icon.on_trash deletes the app's
			# shipped JSON file when developer_mode is on and both are set, and a
			# duplicate whose label scrubs to "ecommerce" would take our own
			# desktop_icon/ecommerce.json with it.
			frappe.db.set_value(
				"Desktop Icon",
				name,
				{"standard": 0, "app": None, "restrict_removal": 0},
				update_modified=False,
			)
			frappe.delete_doc(
				"Desktop Icon",
				name,
				ignore_permissions=True,
				force=True,
				ignore_missing=True,
			)
			print(f"{APP_NAME}: removed duplicate Desktop Icon '{name}'")
		except Exception:
			frappe.log_error(
				title=f"{APP_NAME} enforce_single_desktop_icon: {name}",
				message=frappe.get_traceback(),
			)

	frappe.cache.delete_key("desktop_icons")
	frappe.cache.delete_key("bootinfo")


# --------------------------------------------------------------------------- #
# workspace sidebar
# --------------------------------------------------------------------------- #

# `create_workspace_sidebar_for_workspaces()` (bench install-app, some upgrades)
# makes one Workspace Sidebar per public Workspace, titled after it. Delete the
# workspace later and the sidebar is left behind — and because those auto-created
# records carry no `app` and `standard=0`, frappe.model.sync.remove_orphan_entities()
# can never see them. The result is a second, dead sidebar in the Desk switcher
# alongside the one this app ships.
#
# Only genuine orphans are removed: a sidebar still backing a live Workspace
# belongs to the site, even when Frappe has stamped it with this app's module.
_CANONICAL_SIDEBAR = "Ecommerce"


def _orphan_workspace_sidebars():
	"""Sidebars stamped with this module whose Workspace no longer exists."""
	orphans = []
	rows = frappe.get_all(
		"Workspace Sidebar",
		filters={"module": MODULE_NAME, "for_user": ["in", ["", None]]},
		pluck="name",
	)
	for name in rows:
		if name == _CANONICAL_SIDEBAR:
			continue
		# Auto-created sidebars are titled after their workspace, so a matching
		# Workspace means it is still live.
		if frappe.db.exists("Workspace", name):
			continue
		# ...and honour a renamed one whose Home item still resolves.
		targets = frappe.get_all(
			"Workspace Sidebar Item",
			filters={"parent": name, "link_type": "Workspace"},
			pluck="link_to",
		)
		if any(t and frappe.db.exists("Workspace", t) for t in targets):
			continue
		orphans.append(name)
	return orphans


def enforce_single_workspace_sidebar():
	"""Drop Workspace Sidebars left behind by deleted workspaces."""
	orphans = _orphan_workspace_sidebars()
	if not orphans:
		return

	for name in orphans:
		try:
			# Clear app first: Workspace Sidebar.on_trash deletes the app's shipped
			# JSON when developer_mode is on and `app` is set.
			frappe.db.set_value(
				"Workspace Sidebar", name, {"standard": 0, "app": None}, update_modified=False
			)
			frappe.delete_doc(
				"Workspace Sidebar",
				name,
				ignore_permissions=True,
				force=True,
				ignore_missing=True,
			)
			print(f"{APP_NAME}: removed orphan Workspace Sidebar '{name}'")
		except Exception:
			frappe.log_error(
				title=f"{APP_NAME} enforce_single_workspace_sidebar: {name}",
				message=frappe.get_traceback(),
			)

	frappe.cache.delete_key("bootinfo")
