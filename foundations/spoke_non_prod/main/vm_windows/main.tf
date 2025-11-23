provider "azurerm" {
  resource_provider_registrations = "none"
}

terraform {
  required_providers {
    azurerm = {
      source = "hashicorp/azurerm"
      version = "~>4.0"
    }
  }
}

