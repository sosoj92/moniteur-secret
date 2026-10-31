@echo off
rem Filmer a travers les lunettes : mode lunettes, fond d ecran anime en pause, fenetres reduites. Echap = quitter.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0TOURNAGE.ps1" %*
