"""Azure specific setup CLI."""
import json
from typing import Optional

from censys.cloud_connectors.common.cli.provider_setup import ProviderSetupCli
from censys.cloud_connectors.common.enums import ProviderEnum

from .settings import AzureSpecificSettings


class AzureSetupCli(ProviderSetupCli):
    """Azure provider setup cli command."""

    provider = ProviderEnum.AZURE
    provider_specific_settings_class = AzureSpecificSettings

    def get_subscriptions_from_cli(self) -> list[dict[str, str]]:
        """Get subscriptions from the CLI.

        Returns:
            List[Dict[str, str]]: List of subscriptions.
        """
        try:
            from azure.identity import AzureCliCredential
            from azure.identity._exceptions import CredentialUnavailableError
            from azure.mgmt.resource import SubscriptionClient

            credential = AzureCliCredential()
            subscription_client = SubscriptionClient(credential)
            try:
                subscriptions = [
                    s.as_dict() for s in subscription_client.subscriptions.list()
                ]
                return subscriptions
            except CredentialUnavailableError:
                self.print_warning("Unable to get subscriptions from the CLI")
        except ImportError:
            self.print_warning("Please install the Azure SDK for Python")
        return []

    def prompt_select_subscriptions(
        self, subscriptions: list[dict[str, str]]
    ) -> list[dict[str, str]]:
        """Prompt the user to select subscriptions.

        Args:
            subscriptions (List[Dict[str, str]]): List of subscriptions.

        Returns:
            List[Dict[str, str]]: List of selected subscriptions.
        """
        questions = [
            {
                "type": "checkbox",
                "name": "subscription_ids",
                "message": "Select subscription(s)",
                "choices": [
                    {
                        "name": s.get("display_name"),
                        "value": s.get("subscription_id"),
                    }
                    for s in subscriptions
                    if s.get("state") == "Enabled"
                ],
            }
        ]
        answers = self.prompt(questions)
        selected_subscription_ids = answers.get("subscription_ids", [])
        return [
            s
            for s in subscriptions
            if s.get("subscription_id") in selected_subscription_ids
        ]

    def generate_create_command(self, subscriptions: list[dict[str, str]]) -> str:
        """Generate the command to create a service principal.

        Args:
            subscriptions (List[Dict[str, str]]): List of subscriptions.

        Returns:
            str: Command to create a service principal.
        """
        command = [
            "az",
            "ad",
            "sp",
            "create-for-rbac",
            "--name",
            '"Censys Cloud Connector"',
            "--role",
            "Reader",
            "--output",
            "json",
            "--scopes",
        ]
        for subscription in subscriptions:
            if subscription_id := subscription.get("id"):
                command.append(subscription_id)
        create_command = " ".join(command)
        return create_command

    def create_service_principal(
        self, subscriptions: list[dict[str, str]]
    ) -> Optional[dict]:
        """Create a service principal.

        Args:
            subscriptions (List[Dict[str, str]]): List of subscriptions.

        Returns:
            Optional[dict]: Service principal.
        """
        command = self.generate_create_command(subscriptions)
        self.print_command(command)
        answers = self.prompt(
            [
                {
                    "type": "confirm",
                    "name": "create_service_principal",
                    "message": "Create service principal with above command?",
                    "default": True,
                }
            ]
        )
        if not answers.get("create_service_principal", False):  # pragma: no cover
            self.print_warning(
                "Please manually create a service principal with the role 'Reader'"
            )
            return None

        res = self.run_command(command)
        if res.returncode != 0:
            error = res.stderr.decode("utf-8").strip()
            self.print_error(f"Error creating service principal: {error}")
            return None
        self.print_info("Service principal successfully created!")
        creds = json.loads(res.stdout)
        return creds

    def setup(self):
        """Setup the Azure provider."""
        cli_choice = "Generate with CLI"
        input_choice = "Input existing credentials"
        questions = [
            {
                "type": "list",
                "name": "get_credentials_from",
                "message": "Select a method to configure your credentials",
                "choices": [cli_choice, input_choice],
            }
        ]
        answers = self.prompt(questions)

        get_credentials_from = answers.get("get_credentials_from")
        if get_credentials_from == input_choice:
            # Prompt for credentials
            super().setup()
        elif get_credentials_from == cli_choice:
            subscriptions = self.get_subscriptions_from_cli()
            if len(subscriptions) == 0:
                self.print_error("No subscriptions found")
                exit(1)

            selected_subscriptions = self.prompt_select_subscriptions(subscriptions)
            if len(selected_subscriptions) == 0:
                self.print_error("No subscriptions selected")
                exit(1)

            service_principal = self.create_service_principal(selected_subscriptions)
            if service_principal is None:
                self.print_error(
                    "Service principal not created. Please try again or manually create a service principal"
                )
                exit(1)

            # Save the service principal
            provider_settings = self.provider_specific_settings_class(
                subscription_id=[
                    subscription_id
                    for s in selected_subscriptions
                    if s and (subscription_id := s.get("subscription_id"))
                ],
                tenant_id=service_principal.get("tenant"),
                client_id=service_principal.get("appId"),
                client_secret=service_principal.get("password"),
            )
            # TODO: Confirm that another provider is not already configured for the above subscription IDs
            self.add_provider_specific_settings(provider_settings)
