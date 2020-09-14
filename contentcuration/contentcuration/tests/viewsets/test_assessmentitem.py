from __future__ import absolute_import

import uuid

from django.core.urlresolvers import reverse
from le_utils.constants import content_kinds

from contentcuration import models
from contentcuration.tests import testdata
from contentcuration.tests.base import StudioAPITestCase
from contentcuration.viewsets.sync.constants import ASSESSMENTITEM
from contentcuration.viewsets.sync.utils import generate_copy_event
from contentcuration.viewsets.sync.utils import generate_create_event
from contentcuration.viewsets.sync.utils import generate_delete_event
from contentcuration.viewsets.sync.utils import generate_update_event


class SyncTestCase(StudioAPITestCase):
    @property
    def sync_url(self):
        return reverse("sync")

    @property
    def assessmentitem_metadata(self):
        return {
            "assessment_id": uuid.uuid4().hex,
            "contentnode": models.ContentNode.objects.filter(
                kind_id=content_kinds.EXERCISE
            )
            .first()
            .id,
        }

    @property
    def assessmentitem_db_metadata(self):
        return {
            "assessment_id": uuid.uuid4().hex,
            "contentnode_id": models.ContentNode.objects.filter(
                kind_id=content_kinds.EXERCISE
            )
            .first()
            .id,
        }

    def setUp(self):
        super(SyncTestCase, self).setUp()
        self.channel = testdata.channel()
        self.user = testdata.user()
        self.channel.editors.add(self.user)

    def test_create_assessmentitem(self):
        self.client.force_authenticate(user=self.user)
        assessmentitem = self.assessmentitem_metadata
        response = self.client.post(
            self.sync_url,
            [
                generate_create_event(
                    assessmentitem["assessment_id"], ASSESSMENTITEM, assessmentitem
                )
            ],
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        try:
            models.AssessmentItem.objects.get(
                assessment_id=assessmentitem["assessment_id"]
            )
        except models.AssessmentItem.DoesNotExist:
            self.fail("AssessmentItem was not created")

    def test_create_assessmentitems(self):

        self.client.force_authenticate(user=self.user)
        assessmentitem1 = self.assessmentitem_metadata
        assessmentitem2 = self.assessmentitem_metadata
        response = self.client.post(
            self.sync_url,
            [
                generate_create_event(
                    assessmentitem1["assessment_id"], ASSESSMENTITEM, assessmentitem1
                ),
                generate_create_event(
                    assessmentitem2["assessment_id"], ASSESSMENTITEM, assessmentitem2
                ),
            ],
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        try:
            models.AssessmentItem.objects.get(
                assessment_id=assessmentitem1["assessment_id"]
            )
        except models.AssessmentItem.DoesNotExist:
            self.fail("AssessmentItem 1 was not created")

        try:
            models.AssessmentItem.objects.get(
                assessment_id=assessmentitem2["assessment_id"]
            )
        except models.AssessmentItem.DoesNotExist:
            self.fail("AssessmentItem 2 was not created")

    def test_update_assessmentitem(self):

        assessmentitem = models.AssessmentItem.objects.create(
            **self.assessmentitem_db_metadata
        )
        new_question = "{}"

        self.client.force_authenticate(user=self.user)
        response = self.client.post(
            self.sync_url,
            [
                generate_update_event(
                    assessmentitem.assessment_id,
                    ASSESSMENTITEM,
                    {"question": new_question},
                )
            ],
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(
            models.AssessmentItem.objects.get(id=assessmentitem.id).question,
            new_question,
        )

    def test_update_assessmentitems(self):

        assessmentitem1 = models.AssessmentItem.objects.create(
            **self.assessmentitem_db_metadata
        )
        assessmentitem2 = models.AssessmentItem.objects.create(
            **self.assessmentitem_db_metadata
        )
        new_question = "{}"

        self.client.force_authenticate(user=self.user)
        response = self.client.post(
            self.sync_url,
            [
                generate_update_event(
                    assessmentitem1.assessment_id,
                    ASSESSMENTITEM,
                    {"question": new_question},
                ),
                generate_update_event(
                    assessmentitem2.assessment_id,
                    ASSESSMENTITEM,
                    {"question": new_question},
                ),
            ],
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(
            models.AssessmentItem.objects.get(id=assessmentitem1.id).question,
            new_question,
        )
        self.assertEqual(
            models.AssessmentItem.objects.get(id=assessmentitem2.id).question,
            new_question,
        )

    def test_delete_assessmentitem(self):

        assessmentitem = models.AssessmentItem.objects.create(
            **self.assessmentitem_db_metadata
        )

        self.client.force_authenticate(user=self.user)
        response = self.client.post(
            self.sync_url,
            [generate_delete_event(assessmentitem.assessment_id, ASSESSMENTITEM)],
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        try:
            models.AssessmentItem.objects.get(id=assessmentitem.id)
            self.fail("AssessmentItem was not deleted")
        except models.AssessmentItem.DoesNotExist:
            pass

    def test_delete_assessmentitems(self):
        assessmentitem1 = models.AssessmentItem.objects.create(
            **self.assessmentitem_db_metadata
        )

        assessmentitem2 = models.AssessmentItem.objects.create(
            **self.assessmentitem_db_metadata
        )

        self.client.force_authenticate(user=self.user)
        response = self.client.post(
            self.sync_url,
            [
                generate_delete_event(assessmentitem1.assessment_id, ASSESSMENTITEM),
                generate_delete_event(assessmentitem2.assessment_id, ASSESSMENTITEM),
            ],
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        try:
            models.AssessmentItem.objects.get(id=assessmentitem1.id)
            self.fail("AssessmentItem 1 was not deleted")
        except models.AssessmentItem.DoesNotExist:
            pass

        try:
            models.AssessmentItem.objects.get(id=assessmentitem2.id)
            self.fail("AssessmentItem 2 was not deleted")
        except models.AssessmentItem.DoesNotExist:
            pass

    def test_copy_assessmentitem(self):
        assessmentitem = models.AssessmentItem.objects.create(
            **self.assessmentitem_db_metadata
        )
        new_assessment_id = uuid.uuid4().hex
        self.client.force_authenticate(user=self.user)
        response = self.client.post(
            self.sync_url,
            [
                generate_copy_event(
                    new_assessment_id,
                    ASSESSMENTITEM,
                    assessmentitem.assessment_id,
                    {"contentnode": self.channel.main_tree_id},
                )
            ],
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)

        try:
            new_assessment = models.AssessmentItem.objects.get(
                assessment_id=new_assessment_id
            )
        except models.AssessmentItem.DoesNotExist:
            self.fail("AssessmentItem was not copied")

        self.assertEqual(new_assessment.contentnode_id, self.channel.main_tree_id)


class CRUDTestCase(StudioAPITestCase):
    @property
    def assessmentitem_metadata(self):
        return {
            "assessment_id": uuid.uuid4().hex,
            "contentnode": models.ContentNode.objects.filter(
                kind_id=content_kinds.EXERCISE
            )
            .first()
            .id,
        }

    @property
    def assessmentitem_db_metadata(self):
        return {
            "assessment_id": uuid.uuid4().hex,
            "contentnode_id": models.ContentNode.objects.filter(
                kind_id=content_kinds.EXERCISE
            )
            .first()
            .id,
        }

    def setUp(self):
        super(CRUDTestCase, self).setUp()
        self.channel = testdata.channel()
        self.user = testdata.user()
        self.channel.editors.add(self.user)

    def test_create_assessmentitem(self):
        self.client.force_authenticate(user=self.user)
        assessmentitem = self.assessmentitem_metadata
        response = self.client.post(
            reverse("assessmentitem-list"), assessmentitem, format="json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        try:
            models.AssessmentItem.objects.get(
                assessment_id=assessmentitem["assessment_id"]
            )
        except models.AssessmentItem.DoesNotExist:
            self.fail("AssessmentItem was not created")

    def test_update_assessmentitem(self):
        assessmentitem = models.AssessmentItem.objects.create(
            **self.assessmentitem_db_metadata
        )
        new_question = "{}"

        self.client.force_authenticate(user=self.user)
        response = self.client.patch(
            reverse("assessmentitem-detail", kwargs={"pk": assessmentitem.id}),
            {"question": new_question},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(
            models.AssessmentItem.objects.get(id=assessmentitem.id).question,
            new_question,
        )

    def test_delete_assessmentitem(self):
        assessmentitem = models.AssessmentItem.objects.create(
            **self.assessmentitem_db_metadata
        )

        self.client.force_authenticate(user=self.user)
        response = self.client.delete(
            reverse("assessmentitem-detail", kwargs={"pk": assessmentitem.id})
        )
        self.assertEqual(response.status_code, 204, response.content)
        try:
            models.AssessmentItem.objects.get(id=assessmentitem.id)
            self.fail("AssessmentItem was not deleted")
        except models.AssessmentItem.DoesNotExist:
            pass
