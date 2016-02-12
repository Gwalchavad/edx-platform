import json
import string
import random
from uuid import uuid4

from common.test.test_migrations.utils import TestMigrations
from instructor_task.tests.test_base import InstructorTaskModuleTestCase
from opaque_keys.edx.locations import i4xEncoder, SlashSeparatedCourseKey
from django.contrib.auth.models import User
from lms.djangoapps.instructor_task.models import InstructorTask

TEST_COURSE_KEY = SlashSeparatedCourseKey('edx', '1.23x', 'test_course')

def randomword(length):
    """ Generate random string upon to given length."""
    return ''.join(random.choice(string.lowercase) for i in range(length))

class TestTextFields(TestMigrations, InstructorTaskModuleTestCase):
    """
    Test migration no. 0002_auto_20160208_0810 for InstructorTask model.
    Fields changes from CharField to TextField.
    """
    migrate_from = '0001_initial'
    migrate_to = '0002_auto_20160208_0810'
    app = 'instructor_task'

    def setUpBeforeMigration(self, apps):
        """
        Setup before migration create InstructorTask model entry to verify after migration.
        """
        self.task_input = randomword(250)
        self.task_output = randomword(999)
        self.instructor = User.objects.create(username="instructor", email="instructor@edx.org")
        self.task = InstructorTask.create(
            TEST_COURSE_KEY,
            "dummy type",
            "dummy key",
            self.task_input,
            self.instructor
        )
        self.task.task_output = self.task_output
        self.task.save_now()

    def test_text_fields_migrated(self):
        """
        Verify that data does not loss after migration when model field changes
        from CharField to TextField.
        """
        task_after_migration = InstructorTask.objects.get(id=self.task.id)
        self.assertEqual(task_after_migration.task_input, json.dumps(self.task_input))
        self.assertEqual(task_after_migration.task_output, self.task_output)
