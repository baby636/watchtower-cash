# Generated by Django 3.0.14 on 2021-06-26 07:55

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('main', '0024_auto_20210625_0114'),
    ]

    operations = [
        migrations.CreateModel(
            name='WalletHistory',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
            ],
        ),
        migrations.AddField(
            model_name='transaction',
            name='spending_txid',
            field=models.CharField(blank=True, db_index=True, max_length=70),
        ),
        migrations.AlterField(
            model_name='recipient',
            name='telegram_id',
            field=models.CharField(blank=True, default='', max_length=50),
            preserve_default=False,
        ),
        migrations.AlterField(
            model_name='recipient',
            name='web_url',
            field=models.CharField(blank=True, default='', max_length=300),
            preserve_default=False,
        ),
        migrations.AlterField(
            model_name='token',
            name='name',
            field=models.CharField(blank=True, default='', max_length=200),
            preserve_default=False,
        ),
        migrations.AlterField(
            model_name='token',
            name='token_ticker',
            field=models.CharField(max_length=200),
        ),
        migrations.AlterField(
            model_name='token',
            name='tokenid',
            field=models.CharField(blank=True, db_index=True, default='', max_length=70),
            preserve_default=False,
        ),
        migrations.AlterField(
            model_name='transaction',
            name='source',
            field=models.CharField(db_index=True, default='', max_length=50),
            preserve_default=False,
        ),
        migrations.AlterField(
            model_name='transaction',
            name='txid',
            field=models.CharField(db_index=True, max_length=70),
        ),
        migrations.AlterField(
            model_name='wallet',
            name='wallet_hash',
            field=models.CharField(db_index=True, max_length=70),
        ),
    ]
