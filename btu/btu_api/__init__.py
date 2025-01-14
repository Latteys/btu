""" btu/btu_api """

from functools import partial
import inspect
import os
import pickle
import time

from rq.compat import string_types, as_text

import frappe
from frappe.utils import cstr


class Sanchez():

	def __init__(self):
		self.function_name = None
		self.instance = None
		self.args = ()
		self.kwargs = {}

	def build_internals(self, func, _args, _kwargs):
		"""
		Given a function (probably 'execute_job') and _kwargs, create an RQ job.
		"""
		if inspect.ismethod(func):
			self.instance = func.__self__
			self.function_name = func.__name__
		elif inspect.isfunction(func) or inspect.isbuiltin(func):
			self.function_name = f"{func.__module__}.{func.__qualname__}"
		elif isinstance(func, string_types):
			self.function_name = as_text(func)
		elif not inspect.isclass(func) and hasattr(func, '__call__'):  # a callable class instance
			self._instance = func
			self.function_name = '__call__'
		else:
			raise TypeError(f"Expected a callable or a string, but got: {func}")

		self.args = _args or ()
		self.kwargs = _kwargs or {}

		# NOTE: Important to substitute () or {} instead of None.
		# This prevents errors such as "argument after ** must be a mapping, not NoneType"
		# if self.args is None:
		# self.args = ()
		# if self.kwargs is None:
		#  self.kwargs = {}

	def get_serialized_rq_job(self):
		"""
		Create a tuple of RQ Job 'data', and return in a serialized (pickled) binary format.
		"""
		job_tuple = self.function_name, self.instance, self.args, self.kwargs
		dumps = partial(pickle.dumps, protocol=pickle.HIGHEST_PROTOCOL)  # defines how to do the pickling.
		return dumps(job_tuple)  # this is the serialized/pickled job

# The following function was copied from 'frappe.utils.background_jobs'
# pylint: disable=too-many-branches, inconsistent-return-statements
def execute_job(site, method, event, job_name, kwargs, user=None, is_async=True, retry=0):
	"""
	Executes job in a worker, performs commit/rollback and logs if there is any error
	"""
	if is_async:
		frappe.connect(site)
		if os.environ.get('CI'):
			frappe.flags.in_test = True

		if user:
			frappe.set_user(user)

	if isinstance(method, string_types):
		method_name = method
		method = frappe.get_attr(method)
	else:
		method_name = cstr(method.__name__)

	# VERY IMPORTANT: Substitute {} instead of None for kwargs.
	# This solves the error "argument after ** must be a mapping, not NoneType"
	if kwargs is None:
		kwargs = {}

	frappe.monitor.start("job", method_name, kwargs)
	try:
		method(**kwargs)
	except (frappe.db.InternalError, frappe.RetryBackgroundJobError) as ex:
		frappe.db.rollback()

		if (retry < 5 and
			(isinstance(ex, frappe.RetryBackgroundJobError) or
				(frappe.db.is_deadlocked(ex) or frappe.db.is_timedout(ex)))):
			# retry the job if
			# 1213 = deadlock
			# 1205 = lock wait timeout
			# or RetryBackgroundJobError is explicitly raised
			frappe.destroy()
			time.sleep(retry+1)
			return execute_job(site, method, event, job_name, kwargs,
				is_async=is_async, retry=retry+1)

		frappe.log_error(title=method_name)
		raise

	except:
		frappe.db.rollback()
		frappe.log_error(title=method_name)
		frappe.db.commit()
		print(frappe.get_traceback())
		raise

	else:
		frappe.db.commit()

	finally:
		frappe.monitor.stop()
		if is_async:
			frappe.destroy()
