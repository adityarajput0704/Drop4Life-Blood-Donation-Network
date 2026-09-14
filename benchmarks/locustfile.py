from locust import HttpUser, task, between


class DonorAPIUser(HttpUser):
    wait_time = between(0.1, 0.3)

    @task
    def search_donors(self):
        self.client.get(
            "/donors/",
            params={
                "blood_group": "O_POS",
                "is_available": True,
                "lat": 19.0760,
                "lng": 72.8777,
                "radius_km": 10,
            },
            name="/donors/ - proximity search",
        )