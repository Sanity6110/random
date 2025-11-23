resource "azurerm_storage_account" "storage_account" {
  name                     = var.storage_account_name
  location                 = var.location
  resource_group_name      = var.rg_name
  account_tier             = var.account_tier
  account_replication_type = var.replication_type
}