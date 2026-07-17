app_name = "ecommerce_integration"
app_title = "Ecommerce Integration"
app_publisher = "Upande LTD"
app_description = "Upande Ecommerce Integrations"
app_email = "wilfred@upande.com"
app_license = "mit"

# Send non-GET requests for this app's endpoints as native `application/json`
# bodies instead of form-encoded, per-key JSON-stringified values.
use_json_request_body = True

# Apps
# ------------------

# This app is self-contained: it declares no dependency on any other custom app.
# It reads a few doctypes that upande_webshop also ships (Webshop Item Prices,
# Stem Length Price, Delivery Point, ...) but only when they are present on the
# site — every such read is guarded, so the app installs and runs standalone.
# required_apps = []

# Each item in the list will be shown as an app in the apps page
# add_to_apps_screen = [
# 	{
# 		"name": "ecommerce_integration",
# 		"logo": "/assets/ecommerce_integration/logo.png",
# 		"title": "Ecommerce Integration",
# 		"route": "/ecommerce_integration",
# 		"has_permission": "ecommerce_integration.api.permission.has_app_permission"
# 	}
# ]

# Includes in <head>
# ------------------

# include js, css files in header of desk.html
# app_include_css = "/assets/ecommerce_integration/css/ecommerce_integration.css"
# app_include_js = "/assets/ecommerce_integration/js/ecommerce_integration.js"

# include js, css files in header of web template
# web_include_css = "/assets/ecommerce_integration/css/ecommerce_integration.css"
# web_include_js = "/assets/ecommerce_integration/js/ecommerce_integration.js"

# include custom scss in every website theme (without file extension ".scss")
# website_theme_scss = "ecommerce_integration/public/scss/website"

# include js, css files in header of web form
# webform_include_js = {"doctype": "public/js/doctype.js"}
# webform_include_css = {"doctype": "public/css/doctype.css"}

# include js in page
# page_js = {"page" : "public/js/file.js"}

# include js in doctype views
# Shared Shelf Stock move dialog + inline-button helper (registers the
# `upande_webshop.*` client globals the settings forms call). Copied from
# upande_webshop; the server methods it xcalls still live in upande_webshop.
doctype_js = {
    "Biflorica Setting": "public/js/shelf_move.js",
    "Floriday Settings": "public/js/shelf_move.js",
}
# doctype_js = {"doctype" : "public/js/doctype.js"}
# doctype_list_js = {"doctype" : "public/js/doctype_list.js"}
# doctype_tree_js = {"doctype" : "public/js/doctype_tree.js"}
# doctype_calendar_js = {"doctype" : "public/js/doctype_calendar.js"}

# Svg Icons
# ------------------
# include app icons in desk
# app_include_icons = "ecommerce_integration/public/icons.svg"

# Home Pages
# ----------

# application home page (will override Website Settings)
# home_page = "login"

# website user home page (by Role)
# role_home_page = {
# 	"Role": "home_page"
# }

# Generators
# ----------

# automatically create page for each record of this doctype
# website_generators = ["Web Page"]

# automatically load and sync documents of this doctype from downstream apps
# importable_doctypes = [doctype_1]

# Jinja
# ----------

# add methods and filters to jinja environment
# jinja = {
# 	"methods": "ecommerce_integration.utils.jinja_methods",
# 	"filters": "ecommerce_integration.utils.jinja_filters"
# }

# Installation
# ------------

# before_install = "ecommerce_integration.install.before_install"
after_install = "ecommerce_integration.setup.install.after_install"

# after_migrate runs in this order:
#   1. resync_app_resources — force-reload the JSON resources we ship (workspace,
#      doctypes, ...) bypassing Frappe's modified-timestamp skip.
#   2. normalize_ecommerce_workspace — keep the "Ecommerce" workspace's
#      name/title/label consistent and parent_page clear so /app/ecommerce opens.
#   3. ensure_desktop_icon — upsert the launcher Desktop Icon pointing at
#      /app/ecommerce and drop stale auto-generated ones.
#   4. Floriday + Biflorica resync_scheduled_jobs — restore Scheduled Job Type
#      rows (user-configured per Settings doc, not in scheduler_events) that
#      Frappe's scheduler sync prunes on migrate.
#   5. ensure_biflorica_custom_fields — re-apply Biflorica custom field defs.
after_migrate = [
    "ecommerce_integration.setup.install.resync_app_resources",
    "ecommerce_integration.setup.install.normalize_ecommerce_workspace",
    "ecommerce_integration.setup.install.ensure_desktop_icon",
    "ecommerce_integration.ecommerce_integration.doctype.floriday_settings.floriday_settings.resync_scheduled_jobs",
    "ecommerce_integration.ecommerce_integration.doctype.biflorica_setting.biflorica_setting.resync_scheduled_jobs",
    "ecommerce_integration.ecommerce_integration.doctype.biflorica_setting.biflorica_custom_fields.ensure_biflorica_custom_fields",
]

# Uninstallation
# ------------

# before_uninstall = "ecommerce_integration.uninstall.before_uninstall"
# after_uninstall = "ecommerce_integration.uninstall.after_uninstall"

# Integration Setup
# ------------------
# To set up dependencies/integrations with other apps
# Name of the app being installed is passed as an argument

# before_app_install = "ecommerce_integration.utils.before_app_install"
# after_app_install = "ecommerce_integration.utils.after_app_install"

# Integration Cleanup
# -------------------
# To clean up dependencies/integrations with other apps
# Name of the app being uninstalled is passed as an argument

# before_app_uninstall = "ecommerce_integration.utils.before_app_uninstall"
# after_app_uninstall = "ecommerce_integration.utils.after_app_uninstall"

# Build
# ------------------
# To hook into the build process

# after_build = "ecommerce_integration.build.after_build"

# Desk Notifications
# ------------------
# See frappe.core.notifications.get_notification_config

# notification_config = "ecommerce_integration.notifications.get_notification_config"

# Permissions
# -----------
# Permissions evaluated in scripted ways

# permission_query_conditions = {
# 	"Event": "frappe.desk.doctype.event.event.get_permission_query_conditions",
# }
#
# has_permission = {
# 	"Event": "frappe.desk.doctype.event.event.has_permission",
# }

# Document Events
# ---------------
# Hook on document methods and events

doc_events = {
    "Sales Order": {
        "on_submit": [
            "ecommerce_integration.ecommerce_integration.doctype.biflorica_setting.biflorica_setting.confirm_biflorica_predeal_on_submit",
        ],
    },
}

# Scheduled Tasks
# ---------------

# scheduler_events = {
# 	"all": [
# 		"ecommerce_integration.tasks.all"
# 	],
# 	"daily": [
# 		"ecommerce_integration.tasks.daily"
# 	],
# 	"hourly": [
# 		"ecommerce_integration.tasks.hourly"
# 	],
# 	"weekly": [
# 		"ecommerce_integration.tasks.weekly"
# 	],
# 	"monthly": [
# 		"ecommerce_integration.tasks.monthly"
# 	],
# }

# Testing
# -------

# before_tests = "ecommerce_integration.install.before_tests"

# Extend DocType Class
# ------------------------------
#
# Specify custom mixins to extend the standard doctype controller.
# extend_doctype_class = {
# 	"Task": "ecommerce_integration.custom.task.CustomTaskMixin"
# }

# Overriding Methods
# ------------------------------
#
# override_whitelisted_methods = {
# 	"frappe.desk.doctype.event.event.get_events": "ecommerce_integration.event.get_events"
# }
#
# each overriding function accepts a `data` argument;
# generated from the base implementation of the doctype dashboard,
# along with any modifications made in other Frappe apps
# override_doctype_dashboards = {
# 	"Task": "ecommerce_integration.task.get_dashboard_data"
# }

# exempt linked doctypes from being automatically cancelled
#
# auto_cancel_exempted_doctypes = ["Auto Repeat"]

# Ignore links to specified DocTypes when deleting documents
# -----------------------------------------------------------

# ignore_links_on_delete = ["Communication", "ToDo"]

# Request Events
# ----------------
# before_request = ["ecommerce_integration.utils.before_request"]
# after_request = ["ecommerce_integration.utils.after_request"]

# Job Events
# ----------
# before_job = ["ecommerce_integration.utils.before_job"]
# after_job = ["ecommerce_integration.utils.after_job"]

# after_file_upload = ["ecommerce_integration.utils.after_file_upload"]

# User Data Protection
# --------------------

# user_data_fields = [
# 	{
# 		"doctype": "{doctype_1}",
# 		"filter_by": "{filter_by}",
# 		"redact_fields": ["{field_1}", "{field_2}"],
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_2}",
# 		"filter_by": "{filter_by}",
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_3}",
# 		"strict": False,
# 	},
# 	{
# 		"doctype": "{doctype_4}"
# 	}
# ]

# Authentication and authorization
# --------------------------------

# auth_hooks = [
# 	"ecommerce_integration.auth.validate"
# ]

# Automatically update python controller files with type annotations for this app.
export_python_type_annotations = True

# Require all whitelisted methods to have type annotations
# Disabled: the migrated Floriday/Biflorica controllers are not yet annotated.
require_type_annotated_api_methods = False

# default_log_clearing_doctypes = {
# 	"Logging DocType Name": 30  # days to retain logs
# }

# Translation
# ------------
# List of apps whose translatable strings should be excluded from this app's translations.
# ignore_translatable_strings_from = []

