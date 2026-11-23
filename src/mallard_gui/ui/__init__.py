"""The trame UI."""

from trame_client.widgets.core import AbstractElement

# trame's html elements drop attributes they do not list, accessibility ones included.
for _name in ("aria_label", "role", "data_part"):
    AbstractElement.register_directive(_name, _name.replace("_", "-"))
