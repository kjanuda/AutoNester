"""Deployment guardrails; no external service is needed for these tests."""
import unittest
from fastapi.testclient import TestClient
from main import app

class RailwayDeploymentTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_health(self):
        response = self.client.get('/health')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], 'ok')
        self.assertFalse(response.json()['production_ready'])

    def test_too_large_post_rejected(self):
        response = self.client.post('/api/patterns/import-dxf',
                                    data=b'x' * (10 * 1024 * 1024 + 1),
                                    headers={'Content-Type': 'application/octet-stream'})
        self.assertEqual(response.status_code, 413)

    def test_allowed_local_origin(self):
        response = self.client.options('/api/nesting/generate', headers={
            'Origin': 'http://localhost:3000',
            'Access-Control-Request-Method': 'POST',
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get('access-control-allow-origin'), 'http://localhost:3000')

    def test_unlisted_origin_not_allowed(self):
        response = self.client.options('/api/nesting/generate', headers={
            'Origin': 'https://unrelated.example',
            'Access-Control-Request-Method': 'POST',
        })
        self.assertNotEqual(response.headers.get('access-control-allow-origin'), 'https://unrelated.example')
