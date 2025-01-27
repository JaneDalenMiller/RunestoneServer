# ***************************************
# |docname| - pytest fixtures for testing
# ***************************************
#
# To get started on running tests, see tests/README.rst
#
#  These fixtures start the web2py server then submit requests to it.
#
#  **NOTE:** Make sure you don't have another server running, because it will grab the requests instead of letting the test server respond to requests.
#
# The overall testing approach is functional: rather than test a function, this file primarily tests endpoints on the web server. To accomplish this:
#
# - This file includes the `web2py_server` fixture to start a web2py server, and a fixture (`test_client`) to make requests of it. To make debug easier, the `test_client` class saves the HTML of a failing test to a file, and also saves any web2py tracebacks in the HTML form to a file.
# - The `runestone_db` and related classes provide the ability to access the web2py database directory, in order to set up and tear down test. In order to leave the database unchanged after a test, almost all routines that modify the database are wrapped in a context manager; on exit, then delete any modifications.
#
# .. contents::
#
# Imports
# =======
# These are listed in the order prescribed by `PEP 8
# <http://www.python.org/dev/peps/pep-0008/#imports>`_.
#
# Standard library
# ----------------
import sys
import time
import subprocess
from io import open
import json
import os
import re
from threading import Thread
import datetime
from shutil import rmtree, copytree

# Third-party imports
# -------------------
from gluon.contrib.webclient import WebClient
import gluon.shell
from html5validator.validator import Validator
import pytest
from pyvirtualdisplay import Display
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
import six
from six.moves.urllib.error import HTTPError, URLError
from six.moves.urllib.request import urlopen

# Local imports
# -------------
from .utils import COVER_DIRS, DictToObject
from .ci_utils import xqt, pushd


# Set this to False if you want to turn off all web page validation.
W3_VALIDATE = True


# Pytest setup
# ============
# Add `command-line options <http://doc.pytest.org/en/latest/example/parametrize.html#generating-parameters-combinations-depending-on-command-line>`_.
def pytest_addoption(parser):
    # Per the `API reference <http://doc.pytest.org/en/latest/reference.html#_pytest.hookspec.pytest_addoption>`,
    # options are argparse style.
    parser.addoption(
        "--skipdbinit",
        action="store_true",
        help="Skip initialization of the test database.",
    )
    parser.addoption(
        "--skip_w3_validate",
        action="store_true",
        help="Skip W3C validation of web pages.",
    )


# Output a coverage report when testing is done. See https://docs.pytest.org/en/latest/reference.html#_pytest.hookspec.pytest_terminal_summary.
def pytest_terminal_summary(terminalreporter):
    with pushd("../../.."):
        cp = xqt(
            "{} -m coverage report".format(sys.executable), universal_newlines=True
        )
    terminalreporter.write_line(cp.stdout + cp.stderr)


# Utilities
# =========
# A simple data-struct object.
class _object(object):
    pass


# Create a web2py controller environment. This is taken from pieces of ``gluon.shell.run``. It returns a ``dict`` containing the environment.
def web2py_controller_env(
    # _`application`: The name of the application to run in, as a string.
    application
):

    env = gluon.shell.env(application, import_models=True)
    env.update(gluon.shell.exec_pythonrc())
    return env


# Create a web2py controller environment. Given ``ctl_env = web2py_controller('app_name')``, then  ``ctl_env.db`` refers to the usual DAL object for database access, ``ctl_env.request`` is an (empty) Request object, etc.
def web2py_controller(
    # See env_.
    env
):

    return DictToObject(env)


# Fixtures
# ========
#
# web2py access
# -------------
# These fixtures provide access to the web2py Runestone server and its environment.
@pytest.fixture(scope="session")
def web2py_server_address():
    return "http://127.0.0.1:8000"


# This fixture starts and shuts down the web2py server.
#
# Execute this `fixture <https://docs.pytest.org/en/latest/fixture.html>`_ once per `session <https://docs.pytest.org/en/latest/fixture.html#scope-sharing-a-fixture-instance-across-tests-in-a-class-module-or-session>`_.
@pytest.fixture(scope="session")
def web2py_server(runestone_name, web2py_server_address, pytestconfig):
    password = "pass"

    os.environ["WEB2PY_CONFIG"] = "test"
    # HINT: make sure that ``0.py`` has something like the following, that reads this environment variable:
    #
    # .. code:: Python
    #   :number-lines:
    #
    #   config = environ.get("WEB2PY_CONFIG","production")
    #
    #   if config == "production":
    #       settings.database_uri = environ["DBURL"]
    #   elif config == "development":
    #       settings.database_uri = environ.get("DEV_DBURL")
    #   elif config == "test":
    #       settings.database_uri = environ.get("TEST_DBURL")
    #   else:
    #       raise ValueError("unknown value for WEB2PY_CONFIG")

    # HINT: make sure that you export ``TEST_DBURL`` in your environment; it is
    # not set here because it's specific to the local setup, possibly with a
    # password, and thus can't be committed to the repo.
    assert os.environ["TEST_DBURL"]

    # Extract the components of the DBURL. The expected format is ``postgresql://user:password@netloc/dbname``, a simplified form of the `connection URI <https://www.postgresql.org/docs/9.6/static/libpq-connect.html#LIBPQ-CONNSTRING>`_.
    empty1, postgres_ql, pguser, pgpassword, pgnetloc, dbname, empty2 = re.split(
        "^postgres(ql)?://(.*):(.*)@(.*)/(.*)$", os.environ["TEST_DBURL"]
    )
    assert (not empty1) and (not empty2)
    os.environ["PGPASSWORD"] = pgpassword
    os.environ["PGUSER"] = pguser
    os.environ["DBHOST"] = pgnetloc

    # Assume we are running with working directory in tests.
    if pytestconfig.getoption("skipdbinit"):
        print("Skipping DB initialization.")
    else:
        # In the future, to print the output of the init/build process, see `pytest #1599 <https://github.com/pytest-dev/pytest/issues/1599>`_ for code to enable/disable output capture inside a test.
        #
        # Make sure runestone_test is nice and clean -- this will remove many
        # tables that web2py will then re-create.
        xqt("rsmanage --verbose initdb --reset --force")

        # Copy the test book to the books directory.
        rmtree("../books/test_course_1", ignore_errors=True)
        # Sometimes this fails for no good reason on Windows. Retry.
        for retry in range(100):
            try:
                copytree("test_course_1", "../books/test_course_1")
                break
            except OSError:
                if retry == 99:
                    raise
        # Build the test book to add in db fields needed.
        with pushd("../books/test_course_1"):
            # The runestone build process only looks at ``DBURL``.
            os.environ["DBURL"] = os.environ["TEST_DBURL"]
            xqt(
                "{} -m runestone build --all".format(sys.executable),
                "{} -m runestone deploy".format(sys.executable),
            )

    with pushd("../../.."):
        xqt("{} -m coverage erase".format(sys.executable))

        # For debug, uncomment the next three lines, then run web2py manually to see all debug messages. Use a command line like ``python web2py.py -a pass -X -K runestone,runestone &`` to also start the workers for the scheduler.
        ##import pdb; pdb.set_trace()
        ##yield DictToObject(dict(password=password))
        ##return

        # Start the web2py server and the `web2py scheduler <http://web2py.com/books/default/chapter/29/04/the-core#Scheduler-Deployment>`_.
        web2py_server = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "coverage",
                "run",
                "--append",
                "--source=" + COVER_DIRS,
                "web2py.py",
                "-a",
                password,
                "--nogui",
                "--minthreads=10",
                "--maxthreads=20",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            # Produce text (not binary) output for nice output in ``echo()`` below.
            universal_newlines=True,
        )
        # Wait for the webserver to come up.
        for tries in range(50):
            try:
                urlopen(web2py_server_address, timeout=5)
            except URLError:
                # Wait for the server to come up.
                time.sleep(0.1)
            else:
                # The server is up. We're done.
                break
        # Running two processes doesn't produce two active workers. Running with ``-K runestone,runestone`` means additional subprocesses are launched that we lack the PID necessary to kill. So, just use one worker.
        web2py_scheduler = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "coverage",
                "run",
                "--append",
                "--source=" + COVER_DIRS,
                "web2py.py",
                "-K",
                runestone_name,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        # Start a thread to read web2py output and echo it.
        def echo():
            stdout, stderr = web2py_server.communicate()
            print("\n" "web2py server stdout\n" "--------------------\n")
            print(stdout)
            print("\n" "web2py server stderr\n" "--------------------\n")
            print(stderr)

        echo_thread = Thread(target=echo)
        echo_thread.start()

        # Save the password used.
        web2py_server.password = password
        # Wait for the server to come up. The delay varies; this is a guess.

        # After this comes the `teardown code <https://docs.pytest.org/en/latest/fixture.html#fixture-finalization-executing-teardown-code>`_.
        yield web2py_server

        # Terminate the server and schedulers to give web2py time to shut down gracefully.
        web2py_server.terminate()
        web2py_scheduler.terminate()
        echo_thread.join()


# The name of the Runestone controller. It must be module scoped to allow the ``web2py_server`` to use it.
@pytest.fixture(scope="session")
def runestone_name():
    return "runestone"


# The environment of a web2py controller.
@pytest.fixture
def runestone_env(runestone_name):
    return web2py_controller_env(runestone_name)


# Create fixture providing a web2py controller environment for a Runestone application.
@pytest.fixture
def runestone_controller(runestone_env):
    env = web2py_controller(runestone_env)
    yield env
    # Close the database connection after the test completes.
    env.db.close()


# Database
# --------
# These fixture provide access to a clean instance of the Runestone database.
#
# Provide acess the the Runestone database through a fixture. After a test runs,
# restore the database to its initial state.
@pytest.fixture
def runestone_db(runestone_controller):
    db = runestone_controller.db
    yield db

    # Restore the database state after the test finishes.
    # ----------------------------------------------------
    # Rollback changes, which ensures that any errors in the database connection
    # will be cleared.
    db.rollback()

    # This list was generated by running the following query, taken from
    # https://dba.stackexchange.com/a/173117. Note that the query excludes
    # specific tables, which the ``runestone build`` populates and which
    # should not be modified otherwise. One method to identify these tables
    # which should not be truncated is to run ``pg_dump --data-only
    # $TEST_DBURL > out.sql`` on a clean database, then inspect the output to
    # see which tables have data. It also excludes all the scheduler tables,
    # since truncating these tables makes the process take a lot longer.
    #
    # The query is:
    ## SELECT input_table_name || ',' AS truncate_query FROM(SELECT table_schema || '.' || table_name AS input_table_name FROM information_schema.tables WHERE table_schema NOT IN ('pg_catalog', 'information_schema') AND table_name NOT IN ('questions', 'source_code', 'chapters', 'sub_chapters', 'scheduler_run', 'scheduler_task', 'scheduler_task_deps', 'scheduler_worker') AND table_schema NOT LIKE 'pg_toast%') AS information order by input_table_name;
    db.executesql(
        """TRUNCATE
 public.acerror_log,
 public.assignment_questions,
 public.assignments,
 public.auth_cas,
 public.auth_event,
 public.auth_group,
 public.auth_membership,
 public.auth_permission,
 public.auth_user,
 public.clickablearea_answers,
 public.coach_hints,
 public.code,
 public.codelens_answers,
 public.course_instructor,
 public.course_practice,
 public.courses,
 public.dragndrop_answers,
 public.fitb_answers,
 public.grades,
 public.lp_answers,
 public.lti_keys,
 public.mchoice_answers,
 public.parsons_answers,
 public.payments,
 public.practice_grades,
 public.question_grades,
 public.question_tags,
 public.section_users,
 public.sections,
 public.shortanswer_answers,
 public.sub_chapter_taught,
 public.tags,
 public.timed_exam,
 public.useinfo,
 public.user_biography,
 public.user_chapter_progress,
 public.user_courses,
 public.user_state,
 public.user_sub_chapter_progress,
 public.user_topic_practice,
 public."user_topic_practice_Completion",
 public.user_topic_practice_feedback,
 public.user_topic_practice_log,
 public.user_topic_practice_survey,
 public.web2py_session_runestone CASCADE;
 """
    )
    db.commit()


# Provide context managers for manipulating the Runestone database.
class _RunestoneDbTools(object):
    def __init__(self, runestone_db):
        self.db = runestone_db

    # Create a new course. It returns an object with information about the created course.
    def create_course(
        self,
        # The name of the course to create, as a string.
        course_name="test_child_course_1",
        # The start date of the course, as a string.
        term_start_date="2000-01-01",
        # The value of the ``login_required`` flag for the course.
        login_required=True,
        # The base course for this course. If ``None``, it will use ``course_name``.
        base_course="test_course_1",
        # The student price for this course.
        student_price=None,
    ):

        # Sanity check: this class shouldn't exist.
        db = self.db
        assert not db(db.courses.course_name == course_name).select().first()

        # Create the base course if it doesn't exist.
        if course_name != base_course and not db(
            db.courses.course_name == base_course
        ).select(db.courses.id):
            self.create_course(
                base_course, term_start_date, login_required, base_course, student_price
            )

        # Store these values in an object for convenient access.
        obj = _object()
        obj.course_name = course_name
        obj.term_start_date = term_start_date
        obj.login_required = login_required
        obj.base_course = base_course
        obj.student_price = student_price
        obj.course_id = db.courses.insert(
            course_name=course_name,
            base_course=obj.base_course,
            term_start_date=term_start_date,
            login_required=login_required,
            student_price=student_price,
        )
        db.commit()
        return obj

    def make_instructor(
        self,
        # The ID of the user to make an instructor.
        user_id,
        # The ID of the course in which the user will be an instructor.
        course_id,
    ):

        db = self.db
        course_instructor_id = db.course_instructor.insert(
            course=course_id, instructor=user_id
        )
        db.commit()
        return course_instructor_id


# Present ``_RunestoneDbTools`` as a fixture.
@pytest.fixture
def runestone_db_tools(runestone_db):
    return _RunestoneDbTools(runestone_db)


# HTTP client
# -----------
# Provide access to Runestone through HTTP.
#
# Given the ``test_client.text``, prepare to write it to a file.
def _html_prep(text_str):
    _str = text_str.replace("\r\n", "\n")
    # Deal with fun Python 2 encoding quirk.
    return _str if six.PY2 else _str.encode("utf-8")


# Create a client for accessing the Runestone server.
class _TestClient(WebClient):
    def __init__(
        self,
        web2py_server,
        web2py_server_address,
        runestone_name,
        tmp_path,
        pytestconfig,
    ):
        self.web2py_server = web2py_server
        self.web2py_server_address = web2py_server_address
        self.tmp_path = tmp_path
        self.pytestconfig = pytestconfig
        super(_TestClient, self).__init__(
            "{}/{}/".format(self.web2py_server_address, runestone_name), postbacks=True
        )

    # Use the W3C validator to check the HTML at the given URL.
    def validate(
        self,
        # The relative URL to validate.
        url,
        # An optional string that, if provided, must be in the text returned by the server. If this is a sequence of strings, all of the provided strings must be in the text returned by the server.
        expected_string="",
        # The number of validation errors expected. If None, no validation is performed.
        expected_errors=None,
        # The expected status code from the request.
        expected_status=200,
        # All additional keyword arguments are passed to the ``post`` method.
        **kwargs
    ):

        try:
            try:
                self.post(url, **kwargs)
            except HTTPError as e:
                # If this was the expected result, return.
                if e.code == expected_status:
                    # Since this is an error of some type, these paramets must be empty, since they can't be checked.
                    assert not expected_string
                    assert not expected_errors
                    return ""
                else:
                    raise
            assert self.status == expected_status
            if expected_string:
                if isinstance(expected_string, str):
                    assert expected_string in self.text
                else:
                    # Assume ``expected_string`` is a sequence of strings.
                    assert all(string in self.text for string in expected_string)

            if expected_errors is not None and not self.pytestconfig.getoption(
                "skip_w3_validate"
            ):

                # Redo this section using html5validate command line
                vld = Validator(errors_only=True, stack_size=2048)
                tmpname = self.tmp_path / "tmphtml.html"
                with open(tmpname, "w", encoding="utf8") as f:
                    f.write(self.text)
                errors = vld.validate([str(tmpname)])

                assert errors == expected_errors

            return self.text

        except AssertionError:
            # Save the HTML to make fixing the errors easier. Note that ``self.text`` is already encoded as utf-8.
            validation_file = url.replace("/", "-") + ".html"
            with open(validation_file, "wb") as f:
                f.write(_html_prep(self.text))
            print("Validation failure saved to {}.".format(validation_file))
            raise

        except RuntimeError as e:
            # Provide special handling for web2py exceptions by saving the
            # resulting traceback.
            if e.args[0].startswith("ticket "):
                # Create a client to access the admin interface.
                admin_client = WebClient(
                    "{}/admin/".format(self.web2py_server_address), postbacks=True
                )
                # Log in.
                admin_client.post("", data={"password": self.web2py_server.password})
                assert admin_client.status == 200
                # Get the error.
                error_code = e.args[0][len("ticket ") :]
                admin_client.get("default/ticket/" + error_code)
                assert admin_client.status == 200
                # Save it to a file.
                traceback_file = url.replace("/", "-") + "_traceback.html"
                with open(traceback_file, "wb") as f:
                    f.write(_html_prep(admin_client.text))
                print("Traceback saved to {}.".format(traceback_file))
            raise

    def logout(self):
        self.validate("default/user/logout", "Logged out")

    # Always logout after a test finishes.
    def tearDown(self):
        self.logout()


# Present ``_TestClient`` as a fixure.
@pytest.fixture
def test_client(
    web2py_server, web2py_server_address, runestone_name, tmp_path, pytestconfig
):
    tc = _TestClient(
        web2py_server, web2py_server_address, runestone_name, tmp_path, pytestconfig
    )
    yield tc
    tc.tearDown()


# Provide a method to create a user and perform common user operations.
class _TestUser(object):
    def __init__(
        self,
        # These are fixtures.
        test_client,
        runestone_db_tools,
        # The username for this user.
        username,
        # The password for this user.
        password,
        # The course object returned by ``create_course`` this user will register for.
        course,
        # True if the course is free (no payment required); False otherwise.
        is_free=True,
        # The first name for this user.
        first_name="test",
        # The last name for this user.
        last_name="user",
    ):

        self.test_client = test_client
        self.runestone_db_tools = runestone_db_tools
        self.username = username
        self.first_name = first_name
        self.last_name = last_name
        self.email = self.username + "@foo.com"
        self.password = password
        self.course = course
        self.is_free = is_free

        # Registration doesn't work unless we're logged out.
        self.test_client.logout()
        # Now, post the registration.
        self.test_client.validate(
            "default/user/register",
            "Support Runestone Interactive" if self.is_free else "Payment Amount",
            data=dict(
                username=self.username,
                first_name=self.first_name,
                last_name=self.last_name,
                # The e-mail address must be unique.
                email=self.email,
                password=self.password,
                password_two=self.password,
                # Note that ``course_id`` is (on the form) actually a course name.
                course_id=self.course.course_name,
                accept_tcp="on",
                donate="0",
                _next="/runestone/default/index",
                _formname="register",
            ),
        )

        # Record IDs
        db = self.runestone_db_tools.db
        self.user_id = (
            db(db.auth_user.username == self.username)
            .select(db.auth_user.id)
            .first()
            .id
        )

    def login(self):
        self.test_client.validate(
            "default/user/login",
            data=dict(
                username=self.username, password=self.password, _formname="login"
            ),
        )

    def logout(self):
        self.test_client.logout()

    def make_instructor(self, course_id=None):
        # If ``course_id`` isn't specified, use this user's ``course_id``.
        course_id = course_id or self.course.course_id
        return self.runestone_db_tools.make_instructor(self.user_id, course_id)

    # A context manager to update this user's profile. If a course was added, it returns that course's ID; otherwise, it returns None.
    def update_profile(
        self,
        # This parameter is passed to ``test_client.validate``.
        expected_string=None,
        # An updated username, or ``None`` to use ``self.username``.
        username=None,
        # An updated first name, or ``None`` to use ``self.first_name``.
        first_name=None,
        # An updated last name, or ``None`` to use ``self.last_name``.
        last_name=None,
        # An updated email, or ``None`` to use ``self.email``.
        email=None,
        # An updated last name, or ``None`` to use ``self.course.course_name``.
        course_name=None,
        section="",
        # A shortcut for specifying the ``expected_string``, which only applies if ``expected_string`` is not set. Use ``None`` if a course will not be added, ``True`` if the added course is free, or ``False`` if the added course is paid.
        is_free=None,
        # The value of the ``accept_tcp`` checkbox; provide an empty string to leave unchecked. The default value leaves it checked.
        accept_tcp="on",
    ):

        if expected_string is None:
            if is_free is None:
                expected_string = "Course Selection"
            else:
                expected_string = (
                    "Support Runestone Interactive" if is_free else "Payment Amount"
                )
        username = username or self.username
        first_name = first_name or self.first_name
        last_name = last_name or self.last_name
        email = email or self.email
        course_name = course_name or self.course.course_name

        # Perform the update.
        self.test_client.validate(
            "default/user/profile",
            expected_string,
            data=dict(
                username=username,
                first_name=first_name,
                last_name=last_name,
                email=email,
                # Though the field is ``course_id``, it's really the course name.
                course_id=course_name,
                accept_tcp=accept_tcp,
                section=section,
                _next="/runestone/default/index",
                id=str(self.user_id),
                _formname="auth_user/" + str(self.user_id),
            ),
        )

    # Call this after registering for a new course or adding a new course via ``update_profile`` to pay for the course.
    def make_payment(
        self,
        # The `Stripe test tokens <https://stripe.com/docs/testing#cards>`_ to use for payment.
        stripe_token,
        # The course ID of the course to pay for. None specifies ``self.course.course_id``.
        course_id=None,
    ):

        course_id = course_id or self.course.course_id

        # Get the signature from the HTML of the payment page.
        self.test_client.validate("default/payment")
        match = re.search(
            '<input type="hidden" name="signature" value="([^ ]*)" />',
            self.test_client.text,
        )
        signature = match.group(1)

        html = self.test_client.validate(
            "default/payment", data=dict(stripeToken=stripe_token, signature=signature)
        )
        assert ("Thank you for your payment" in html) or ("Payment failed" in html)

    def hsblog(self, **kwargs):
        # Get the time, rounded down to a second, before posting to the server.
        ts = datetime.datetime.utcnow()
        ts -= datetime.timedelta(microseconds=ts.microsecond)

        if "course" not in kwargs:
            kwargs["course"] = self.course.course_name

        if "answer" not in kwargs and "act" in kwargs:
            kwargs["answer"] = kwargs["act"]
        # Post to the server.
        return json.loads(self.test_client.validate("ajax/hsblog", data=kwargs))


# Present ``_TestUser`` as a fixture.
@pytest.fixture
def test_user(test_client, runestone_db_tools):
    return lambda *args, **kwargs: _TestUser(
        test_client, runestone_db_tools, *args, **kwargs
    )


# Provide easy access to a test user and course.
@pytest.fixture
def test_user_1(runestone_db_tools, test_user):
    course = runestone_db_tools.create_course()
    return test_user("test_user_1", "password_1", course)


class _TestAssignment(object):
    assignment_count = 0

    def __init__(
        self,
        test_client,
        test_user,
        runestone_db_tools,
        aname,
        course,
        is_visible=False,
    ):
        self.test_client = test_client
        self.runestone_db_tools = runestone_db_tools
        self.assignment_name = aname
        self.course = course
        self.description = "default description"
        self.is_visible = is_visible
        self.due = datetime.datetime.utcnow() + datetime.timedelta(days=7)
        self.assignment_instructor = test_user(
            "assign_instructor_{}".format(_TestAssignment.assignment_count),
            "password",
            course,
        )
        self.assignment_instructor.make_instructor()
        self.assignment_instructor.login()
        self.assignment_id = json.loads(
            self.test_client.validate(
                "admin/createAssignment", data={"name": self.assignment_name}
            )
        )[self.assignment_name]
        assert self.assignment_id
        _TestAssignment.assignment_count += 1

    def addq_to_assignment(self, **kwargs):
        if "points" not in kwargs:
            kwargs["points"] = 1
        kwargs["assignment"] = self.assignment_id
        res = self.test_client.validate(
            "admin/add__or_update_assignment_question", data=kwargs
        )
        res = json.loads(res)
        assert res["status"] == "success"

    def autograde(self, sid=None):
        print("autograding", self.assignment_name)
        vars = dict(assignment=self.assignment_name)
        if sid:
            vars["sid"] = sid
        res = json.loads(self.test_client.validate("assignments/autograde", data=vars))
        assert res["message"].startswith("autograded")
        return res

    def questions(self):
        """
        Return a list of all (id, name) values for each question
        in an assignment
        """

        db = self.runestone_db_tools.db
        a_q_rows = db(
            (db.assignment_questions.assignment_id == self.assignment_id)
            & (db.assignment_questions.question_id == db.questions.id)
        ).select(orderby=db.assignment_questions.sorting_priority)
        res = []
        for row in a_q_rows:
            res.append(tuple([row.questions.id, row.questions.name]))

        return res

    def calculate_totals(self):
        assert json.loads(
            self.test_client.validate(
                "assignments/calculate_totals",
                data=dict(assignment=self.assignment_name),
            )
        )["success"]

    def make_visible(self):
        self.is_visible = True
        self.save_assignment()

    def set_duedate(self, newdeadline):
        """
        the newdeadline should be a datetime object
        """
        self.due = newdeadline
        self.save_assignment()

    def save_assignment(self):
        assert (
            json.loads(
                self.test_client.validate(
                    "admin/save_assignment",
                    data=dict(
                        assignment_id=self.assignment_id,
                        visible="T" if self.is_visible else "F",
                        description=self.description,
                        due=str(self.due),
                    ),
                )
            )["status"]
            == "success"
        )

    def release_grades(self):
        self.test_client.post(
            "admin/releasegrades",
            data=dict(assignmentid=self.assignment_id, released="yes"),
        )
        assert self.test_client.text == "Success"


@pytest.fixture
def test_assignment(test_client, test_user, runestone_db_tools):
    return lambda *args, **kwargs: _TestAssignment(
        test_client, test_user, runestone_db_tools, *args, **kwargs
    )


# Selenium
# --------
# Provide access to Runestone through a web browser using Selenium.
#
# Create an instance of Selenium once per testing session.
@pytest.fixture(scope="session")
def selenium_driver_session():
    # Start a virtual display for Linux.
    if sys.platform.startswith("linux"):
        display = Display(visible=0, size=(1280, 1024))
        display.start()
    else:
        display = None

    # Start up the Selenium driver.
    options = Options()
    options.add_argument("--window-size=1200,800")
    # When run as root, Chrome complains ``Running as root without --no-sandbox is not supported. See https://crbug.com/638180.`` Here's a `crude check for being root <https://stackoverflow.com/a/52621917>`_.
    if os.geteuid() == 0:
        options.add_argument("--no-sandbox")
    driver = webdriver.Chrome(options=options)

    yield driver

    # Shut everything down.
    driver.quit()
    if display:
        display.stop()


# Provide a way to reset the state of Selenium for each test, without exiting/restarting the driver (which is slow).
@pytest.fixture()
def selenium_driver(selenium_driver_session):
    driver = selenium_driver_session
    # Add an `implicit wait <https://selenium-python.readthedocs.io/waits.html#implicit-waits>`_.
    driver.implicitly_wait(5)

    yield driver

    # Clear as much as possible, to present an almost-fresh instance of a browser for the next test. (Shutting down then starting up a browswer is very slow.)
    driver.execute_script("window.localStorage.clear();")
    driver.execute_script("window.sessionStorage.clear();")
    driver.delete_all_cookies()


@pytest.fixture()
def runestone_selenium_driver(selenium_driver, web2py_server_address, runestone_name):
    # A helper function to attach to the Selenium driver: get from a URL relative to the Runestone application.
    def _rs_get(self, relative_url):
        return self.get(
            "{}/{}/{}".format(web2py_server_address, runestone_name, relative_url)
        )

    # Add the rs_get function to the driver. See https://stackoverflow.com/a/28060251.
    selenium_driver.rs_get = _rs_get.__get__(selenium_driver)
    return selenium_driver


# Provide basic Selenium-based user operations.
class _SeleniumUser:
    def __init__(
        self,
        # These are fixtures.
        runestone_selenium_driver,
        # The username for this user.
        username,
        # The password for this user.
        password,
    ):

        self.driver = runestone_selenium_driver
        self.username = username
        self.password = password

    def login(self):
        self.driver.rs_get("default/user/login")
        self.driver.find_element_by_id("auth_user_username").send_keys(self.username)
        self.driver.find_element_by_id("auth_user_password").send_keys(self.password)
        self.driver.find_element_by_id("login_button").click()

    def logout(self):
        self.driver.rs_get("default/user/logout")
        # See https://selenium-python.readthedocs.io/api.html?highlight=page_source#selenium.webdriver.remote.webdriver.WebDriver.page_source.
        assert "Logged out" in self.driver.page_source


# Present ``_SeleniumUser`` as a fixture. To use, provide it with a ``_TestUser`` instance.
@pytest.fixture
def selenium_user(runestone_selenium_driver):
    return lambda test_user: _SeleniumUser(
        runestone_selenium_driver, test_user.username, test_user.password
    )
