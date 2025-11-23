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

module "storage_account" {
  source = "../../../resource_modules/storage_account"
    storage_account_name         = var.tfvars.storage_account_name
    account_replication_type     = var.tfvas.account_replication_type
}